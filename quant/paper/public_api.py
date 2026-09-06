"""Versioned, tenant-scoped public paper API backend and contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from quant.core.db import postgres_connection
from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    ExposureEffect,
    UnifiedAdmissionService,
    build_admission_runtime,
    order_exposure_effect,
)
from quant.simulator.operations import (
    PositionOperationIntent,
    PositionOperationReservationError,
    PositionOperationState,
    PositionOperationType,
    PostgresPositionOperationStore,
)

from .admin_service import (
    PaperAdminConflict,
    PaperAdminError,
    PaperAdminNotFound,
    PaperAdminValidationError,
    PostgresPaperAdminService,
)
from .conditional_orders import (
    ConditionalOrderConflict,
    ConditionalOrderError,
    ConditionalOrderNotFound,
    ConditionalOrderValidationError,
    PostgresConditionalOrderService,
)
from .live_shadow_store import LiveShadowStore
from .professional_pnl import build_professional_pnl_report, nav_point_from_snapshot
from .position_economics import reconstruct_position_economics
from .retail_platform import PostgresRetailPaperService, retail_identity_key
from .replay_service import (
    PostgresReplayService,
    ReplayCapacityExceeded,
    ReplayNotFound,
    ReplayServiceError,
    ReplayStateConflict,
    ReplayValidationError,
    parse_replay_timestamp,
)
from .scenario_service import (
    PostgresScenarioService,
    ScenarioNotFound,
    ScenarioServiceError,
    ScenarioValidationError,
)
from .tenant_platform import (
    PaperAuthorizationError,
    PaperPermission,
    PaperQuotaExceeded,
    PostgresTenantPlatformStore,
    QuotaMetric,
    TenantPlatformError,
    TenantPrincipal,
    TenantScopeError,
)

API_VERSION = "2026-08-16"
API_KEY_PREFIX = "ppk"
ALLOWED_API_SCOPES = frozenset(
    {
        "paper:read",
        "paper:trade",
        "paper:accounts:write",
        "paper:admin",
    }
)


class PaperApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status_code = int(status_code)
        self.details = dict(details or {})


@dataclass(frozen=True)
class ApiIdentity:
    principal: TenantPrincipal
    api_key_id: UUID
    key_prefix: str
    scopes: frozenset[str]

    def require_scope(self, scope: str) -> None:
        read_implied = scope == "paper:read" and bool(
            self.scopes & {"paper:trade", "paper:accounts:write", "paper:admin"}
        )
        if (
            scope not in self.scopes
            and "paper:admin" not in self.scopes
            and not read_implied
        ):
            raise PaperApiError(
                "PAPER_FORBIDDEN",
                f"API key lacks required scope: {scope}",
                status_code=403,
            )


@dataclass(frozen=True)
class IdempotencyClaim:
    operation: str
    idempotency_key: str
    request_hash: str
    lease_owner: UUID | None
    replayed: bool
    response_status: int | None = None
    response_body: Mapping[str, Any] | None = None


def load_api_key_pepper(environ: Mapping[str, str] | None = None) -> bytes:
    env = environ or os.environ
    credential_file = str(env.get("PAPER_API_KEY_PEPPER_FILE") or "").strip()
    if credential_file:
        value = Path(credential_file).read_text(encoding="utf-8").strip()
    else:
        value = str(env.get("PAPER_API_KEY_PEPPER") or "").strip()
    if len(value) < 32:
        raise PaperApiError(
            "PAPER_API_UNAVAILABLE",
            "paper API key pepper is not configured",
            status_code=503,
        )
    return value.encode("utf-8")


def load_artifact_signing_key(environ: Mapping[str, str] | None = None) -> bytes:
    env = environ or os.environ
    credential_file = str(env.get("PAPER_ARTIFACT_SIGNING_KEY_FILE") or "").strip()
    if credential_file:
        value = Path(credential_file).read_text(encoding="utf-8").strip()
    else:
        value = str(env.get("PAPER_ARTIFACT_SIGNING_KEY") or "").strip()
    if value and len(value) < 32:
        raise PaperApiError(
            "PAPER_API_UNAVAILABLE",
            "paper artifact signing key must contain at least 32 characters",
            status_code=503,
        )
    return value.encode("utf-8")


def issue_api_token(
    tenant_id: UUID,
    api_key_id: UUID,
    *,
    secret: str | None = None,
) -> str:
    selected_secret = secret or secrets.token_urlsafe(32)
    if len(selected_secret) < 24 or "." in selected_secret:
        raise ValueError("API key secret is invalid")
    return f"{API_KEY_PREFIX}_{tenant_id.hex}.{api_key_id.hex}.{selected_secret}"


def parse_api_token(token: str) -> tuple[UUID, UUID]:
    text = str(token).strip()
    try:
        namespace, secret = text.rsplit(".", 1)
        prefix_and_tenant, key_hex = namespace.split(".", 1)
        prefix, tenant_hex = prefix_and_tenant.split("_", 1)
        tenant_id = UUID(hex=tenant_hex)
        api_key_id = UUID(hex=key_hex)
    except (ValueError, TypeError) as exc:
        raise PaperApiError(
            "PAPER_AUTH_INVALID",
            "invalid paper API key",
            status_code=401,
        ) from exc
    if prefix != API_KEY_PREFIX or len(secret) < 24:
        raise PaperApiError(
            "PAPER_AUTH_INVALID",
            "invalid paper API key",
            status_code=401,
        )
    return tenant_id, api_key_id


def hash_api_token(token: str, pepper: bytes) -> str:
    return hmac.new(pepper, str(token).encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            "invalid pagination cursor",
            status_code=400,
        ) from exc
    if not isinstance(payload, dict):
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            "invalid pagination cursor",
            status_code=400,
        )
    return payload


def normalize_limit(value: Any, *, default: int = 50, maximum: int = 200) -> int:
    try:
        selected = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            "limit must be an integer",
            status_code=400,
        ) from exc
    if selected < 1 or selected > maximum:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"limit must be between 1 and {maximum}",
            status_code=400,
        )
    return selected


def require_idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if len(key) < 8 or len(key) > 160:
        raise PaperApiError(
            "PAPER_IDEMPOTENCY_REQUIRED",
            "Idempotency-Key must contain 8 to 160 characters",
            status_code=400,
        )
    return key


class PostgresPaperApiBackend:
    def __init__(
        self,
        *,
        connection_factory: Any = postgres_connection,
        pepper: bytes | None = None,
        tenant_store: PostgresTenantPlatformStore | None = None,
        live_store: LiveShadowStore | None = None,
        replay_service: PostgresReplayService | None = None,
        scenario_service: PostgresScenarioService | None = None,
        conditional_service: PostgresConditionalOrderService | None = None,
        admin_service: PostgresPaperAdminService | None = None,
        retail_service: PostgresRetailPaperService | None = None,
        position_operation_store: PostgresPositionOperationStore | None = None,
        artifact_signing_key: bytes | None = None,
        unified_admission_service: UnifiedAdmissionService | None = None,
        unified_admission_shadow: bool | None = None,
        unified_admission_enforce: bool | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.pepper = pepper if pepper is not None else load_api_key_pepper()
        self.tenant_store = tenant_store or PostgresTenantPlatformStore(
            connection_factory
        )
        self.live_store = live_store or LiveShadowStore(connection_factory)
        self.replay_service = replay_service or PostgresReplayService(self.tenant_store)
        self.scenario_service = scenario_service or PostgresScenarioService(
            self.tenant_store
        )
        self.conditional_service = (
            conditional_service or PostgresConditionalOrderService(self.tenant_store)
        )
        self.admin_service = admin_service or PostgresPaperAdminService(
            self.tenant_store,
            live_store=self.live_store,
            signing_key=(
                artifact_signing_key
                if artifact_signing_key is not None
                else load_artifact_signing_key()
            ),
        )
        self.retail_service = retail_service or PostgresRetailPaperService(
            connection_factory, tenant_store=self.tenant_store
        )
        self.position_operation_store = (
            position_operation_store
            or PostgresPositionOperationStore(connection_factory)
        )
        self._retail_schema_ready = False
        if (
            unified_admission_shadow is None
            and unified_admission_enforce is None
            and unified_admission_service is None
        ):
            admission_runtime = build_admission_runtime(
                connection_factory=connection_factory
            )
            unified_admission_service = admission_runtime.service
            unified_admission_shadow = admission_runtime.shadow
            unified_admission_enforce = admission_runtime.enforce
        self.unified_admission_service = unified_admission_service
        self.unified_admission_shadow = bool(unified_admission_shadow)
        self.unified_admission_enforce = bool(unified_admission_enforce)

    def ensure_retail_schema(self) -> None:
        if self._retail_schema_ready:
            return
        from .paper_ledger import PostgresPaperLedgerSink

        self.tenant_store.ensure_schema()
        PostgresPaperLedgerSink(
            self.connection_factory,
            ensure_schema=True,
        )
        self.retail_service.ensure_schema()
        self.position_operation_store.ensure_schema()
        self._retail_schema_ready = True

    def provision_retail_identity(
        self,
        *,
        provider: str,
        provider_subject: str,
        display_name: str,
        wallet_address: str | None = None,
    ) -> dict[str, Any]:
        """Create or restore one chain-off retail wallet and browser session key."""

        self.ensure_retail_schema()
        selected_provider = str(provider).upper()
        if selected_provider not in {"GUEST", "EVM"}:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "retail identity provider must be GUEST or EVM",
                status_code=400,
            )
        identity_key = retail_identity_key(selected_provider, provider_subject)
        principal = self.tenant_store.bootstrap_tenant(
            tenant_name=f"Paper user {identity_key[:10]}",
            owner_email=f"paper-{identity_key[:24]}@identity.invalid",
            owner_display_name=str(display_name).strip() or "Paper trader",
            idempotency_key=f"retail:{identity_key}",
        )
        wallet = self.retail_service.provision_default_wallet(
            principal,
            display_name=str(display_name).strip() or "My Paper Wallet",
            provider=selected_provider,
            provider_subject=str(provider_subject),
            wallet_address=wallet_address,
        )
        key = self.create_api_key(
            principal,
            name="retail-browser-session",
            scopes=("paper:read", "paper:trade", "paper:accounts:write"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        return {
            "wallet": wallet,
            "token": key["token"],
            "session_expires_at": key["expires_at"],
            "provider": selected_provider,
        }

    def get_retail_session(self, identity: ApiIdentity) -> dict[str, Any]:
        identity.require_scope("paper:read")
        self.ensure_retail_schema()
        wallet = self.retail_service.get_default_wallet(identity.principal)
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT user_id,display_name,status,created_at
                FROM quant.paper_users
                WHERE tenant_id=%s AND user_id=%s
                """,
                (
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                ),
            )
            user = dict(cur.fetchone())
            conn.commit()
        return {"user": user, "wallet": wallet}

    def authenticate(self, token: str) -> ApiIdentity:
        tenant_id, api_key_id = parse_api_token(token)
        token_hash = hash_api_token(token, self.pepper)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_tenant_id', %s, true)",
                (str(tenant_id),),
            )
            cur.execute(
                """
                SELECT key.api_key_id,key.user_id,key.key_prefix,key.key_hash,
                       key.scopes,key.status,key.expires_at,
                       membership.role,membership.status AS membership_status,
                       usr.status AS user_status,tenant.status AS tenant_status
                FROM quant.paper_api_keys key
                JOIN quant.paper_memberships membership
                  ON membership.tenant_id=key.tenant_id
                 AND membership.user_id=key.user_id
                JOIN quant.paper_users usr
                  ON usr.tenant_id=key.tenant_id AND usr.user_id=key.user_id
                JOIN quant.paper_tenants tenant
                  ON tenant.tenant_id=key.tenant_id
                WHERE key.tenant_id=%s AND key.api_key_id=%s
                FOR UPDATE OF key
                """,
                (tenant_id, api_key_id),
            )
            row = cur.fetchone()
            valid = (
                row is not None
                and str(row["status"]) == "ACTIVE"
                and str(row["membership_status"]) == "ACTIVE"
                and str(row["user_status"]) == "ACTIVE"
                and str(row["tenant_status"]) in {"ACTIVE", "FROZEN"}
                and (
                    row["expires_at"] is None
                    or row["expires_at"] > datetime.now(timezone.utc)
                )
                and hmac.compare_digest(str(row["key_hash"]), token_hash)
            )
            if not valid:
                conn.rollback()
                raise PaperApiError(
                    "PAPER_AUTH_INVALID",
                    "invalid or inactive paper API key",
                    status_code=401,
                )
            cur.execute(
                """
                UPDATE quant.paper_api_keys SET last_used_at=clock_timestamp()
                WHERE tenant_id=%s AND api_key_id=%s
                """,
                (tenant_id, api_key_id),
            )
            conn.commit()
            return ApiIdentity(
                principal=TenantPrincipal(
                    tenant_id=tenant_id,
                    actor_user_id=UUID(str(row["user_id"])),
                ),
                api_key_id=api_key_id,
                key_prefix=str(row["key_prefix"]),
                scopes=frozenset(str(scope) for scope in row["scopes"] or []),
            )

    def create_api_key(
        self,
        principal: TenantPrincipal,
        *,
        name: str,
        scopes: Sequence[str],
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        selected_name = str(name).strip()
        if not selected_name:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "API key name is required",
                status_code=400,
            )
        selected_scopes = frozenset(str(scope).strip() for scope in scopes)
        invalid = sorted(selected_scopes - ALLOWED_API_SCOPES)
        if invalid or not selected_scopes:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "one or more API key scopes are invalid",
                status_code=400,
                details={"invalid_scopes": invalid},
            )
        api_key_id = uuid4()
        token = issue_api_token(principal.tenant_id, api_key_id)
        token_namespace, token_secret = token.rsplit(".", 1)
        key_prefix = f"{token_namespace}.{token_secret[:8]}"
        with self.tenant_store._transaction(
            principal, PaperPermission.TENANT_ADMIN
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_api_keys (
                    api_key_id,tenant_id,user_id,name,key_prefix,key_hash,
                    scopes,expires_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    api_key_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    selected_name,
                    key_prefix,
                    hash_api_token(token, self.pepper),
                    sorted(selected_scopes),
                    expires_at,
                ),
            )
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="API_KEY_CREATED",
                resource_type="API_KEY",
                resource_id=str(api_key_id),
                reason="paper_public_api_credential",
                payload={"name": selected_name, "scopes": sorted(selected_scopes)},
            )
            conn.commit()
        return {
            "api_key_id": api_key_id,
            "tenant_id": principal.tenant_id,
            "name": selected_name,
            "key_prefix": key_prefix,
            "scopes": sorted(selected_scopes),
            "expires_at": expires_at,
            "token": token,
        }

    def revoke_api_key(
        self,
        identity: ApiIdentity,
        api_key_id: UUID,
    ) -> bool:
        identity.require_scope("paper:admin")
        return self.revoke_api_key_for_principal(identity.principal, api_key_id)

    def revoke_api_key_for_principal(
        self,
        principal: TenantPrincipal,
        api_key_id: UUID,
    ) -> bool:
        with self.tenant_store._transaction(
            principal, PaperPermission.TENANT_ADMIN
        ) as (conn, cur, _):
            cur.execute(
                """
                UPDATE quant.paper_api_keys
                SET status='REVOKED',revoked_at=clock_timestamp()
                WHERE tenant_id=%s AND api_key_id=%s AND status='ACTIVE'
                RETURNING api_key_id
                """,
                (principal.tenant_id, api_key_id),
            )
            changed = cur.fetchone() is not None
            if changed:
                self.tenant_store._append_audit(
                    cur,
                    principal,
                    event_type="API_KEY_REVOKED",
                    resource_type="API_KEY",
                    resource_id=str(api_key_id),
                    reason="paper_public_api_credential_revocation",
                    payload={},
                )
            conn.commit()
            return changed

    def charge_request_quota(
        self,
        identity: ApiIdentity,
        *,
        request_id: UUID,
    ) -> Any:
        return self.tenant_store.require_quota(
            identity.principal,
            metric=QuotaMetric.TENANT_API_REQUESTS,
            subject_type="TENANT",
            subject_id=str(identity.principal.tenant_id),
            amount=Decimal(1),
            idempotency_key=f"api-request:{request_id}",
            permission=PaperPermission.ACCOUNT_READ,
        )

    def claim_idempotency(
        self,
        identity: ApiIdentity,
        *,
        operation: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        lease_seconds: int = 30,
    ) -> IdempotencyClaim:
        key = require_idempotency_key(idempotency_key)
        request_hash = canonical_request_hash(payload)
        lease_owner = uuid4()
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_api_idempotency (
                    tenant_id,user_id,operation,idempotency_key,request_hash,
                    lease_owner,lease_until
                ) VALUES (%s,%s,%s,%s,%s,%s,
                          clock_timestamp()+make_interval(secs=>%s))
                ON CONFLICT DO NOTHING
                RETURNING state
                """,
                (
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                    str(operation),
                    key,
                    request_hash,
                    lease_owner,
                    max(5, int(lease_seconds)),
                ),
            )
            inserted = cur.fetchone() is not None
            cur.execute(
                """
                SELECT request_hash,state,lease_owner,lease_until,
                       response_status,response_body,last_error_code
                FROM quant.paper_api_idempotency
                WHERE tenant_id=%s AND user_id=%s AND operation=%s
                  AND idempotency_key=%s
                FOR UPDATE
                """,
                (
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                    str(operation),
                    key,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise TenantPlatformError("idempotency claim disappeared")
            if str(row["request_hash"]) != request_hash:
                raise PaperApiError(
                    "PAPER_IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was reused with a different request",
                    status_code=409,
                )
            if str(row["state"]) == "COMPLETED":
                conn.commit()
                return IdempotencyClaim(
                    operation=str(operation),
                    idempotency_key=key,
                    request_hash=request_hash,
                    lease_owner=None,
                    replayed=True,
                    response_status=int(row["response_status"]),
                    response_body=dict(row["response_body"] or {}),
                )
            now = datetime.now(timezone.utc)
            if (
                not inserted
                and row["lease_until"] is not None
                and row["lease_until"] > now
                and UUID(str(row["lease_owner"])) != lease_owner
            ):
                raise PaperApiError(
                    "PAPER_IDEMPOTENCY_IN_PROGRESS",
                    "an identical request is still in progress",
                    status_code=409,
                )
            cur.execute(
                """
                UPDATE quant.paper_api_idempotency
                SET state='PENDING',lease_owner=%s,
                    lease_until=clock_timestamp()+make_interval(secs=>%s),
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND user_id=%s AND operation=%s
                  AND idempotency_key=%s
                """,
                (
                    lease_owner,
                    max(5, int(lease_seconds)),
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                    str(operation),
                    key,
                ),
            )
            conn.commit()
            return IdempotencyClaim(
                operation=str(operation),
                idempotency_key=key,
                request_hash=request_hash,
                lease_owner=lease_owner,
                replayed=False,
            )

    def complete_idempotency(
        self,
        identity: ApiIdentity,
        claim: IdempotencyClaim,
        *,
        status_code: int,
        response_body: Mapping[str, Any],
    ) -> None:
        if claim.replayed or claim.lease_owner is None:
            return
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                UPDATE quant.paper_api_idempotency
                SET state='COMPLETED',response_status=%s,response_body=%s::jsonb,
                    completed_at=clock_timestamp(),updated_at=clock_timestamp(),
                    lease_until=NULL
                WHERE tenant_id=%s AND user_id=%s AND operation=%s
                  AND idempotency_key=%s AND lease_owner=%s AND state='PENDING'
                """,
                (
                    int(status_code),
                    json.dumps(response_body, sort_keys=True, default=str),
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                    claim.operation,
                    claim.idempotency_key,
                    claim.lease_owner,
                ),
            )
            if int(cur.rowcount or 0) != 1:
                raise PaperApiError(
                    "PAPER_IDEMPOTENCY_LOST",
                    "idempotency lease was lost before completion",
                    status_code=409,
                )
            conn.commit()

    def fail_idempotency(
        self,
        identity: ApiIdentity,
        claim: IdempotencyClaim,
        *,
        error_code: str,
    ) -> None:
        if claim.replayed or claim.lease_owner is None:
            return
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                UPDATE quant.paper_api_idempotency
                SET state='FAILED',last_error_code=%s,lease_until=NULL,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND user_id=%s AND operation=%s
                  AND idempotency_key=%s AND lease_owner=%s
                """,
                (
                    str(error_code),
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                    claim.operation,
                    claim.idempotency_key,
                    claim.lease_owner,
                ),
            )
            conn.commit()

    def list_accounts(
        self,
        identity: ApiIdentity,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        decoded = decode_cursor(cursor)
        cursor_account = UUID(str(decoded["account_id"])) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT registry.account_id,registry.name,registry.status,
                       registry.base_currency,registry.current_generation,
                       registry.created_at,ledger.initial_cash,
                       ledger.cash_balance,ledger.cash_reserved,
                       ledger.realized_pnl,
                       strategy.strategy_id AS default_strategy_id
                FROM quant.paper_account_registry registry
                JOIN quant.paper_accounts ledger
                  ON ledger.strategy_id=registry.ledger_strategy_id
                LEFT JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=registry.tenant_id
                 AND strategy.account_id=registry.account_id
                 AND strategy.idempotency_key='default'
                WHERE registry.tenant_id=%s
                  AND (%s::uuid IS NULL OR registry.account_id>%s)
                ORDER BY registry.account_id
                LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    cursor_account,
                    cursor_account,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor({"account_id": str(items[-1]["account_id"])})
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_account(self, identity: ApiIdentity, account_id: UUID) -> dict[str, Any]:
        identity.require_scope("paper:read")
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT registry.account_id,registry.name,registry.status,
                       registry.base_currency,registry.current_generation,
                       registry.created_at,ledger.initial_cash,
                       ledger.cash_balance,ledger.cash_reserved,
                       ledger.realized_pnl,
                       strategy.strategy_id AS default_strategy_id
                FROM quant.paper_account_registry registry
                JOIN quant.paper_accounts ledger
                  ON ledger.strategy_id=registry.ledger_strategy_id
                LEFT JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=registry.tenant_id
                 AND strategy.account_id=registry.account_id
                 AND strategy.idempotency_key='default'
                WHERE registry.tenant_id=%s AND registry.account_id=%s
                """,
                (identity.principal.tenant_id, account_id),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise PaperApiError(
                "PAPER_ACCOUNT_NOT_FOUND",
                "paper account was not found",
                status_code=404,
            )
        return dict(row)

    def create_account(
        self,
        identity: ApiIdentity,
        *,
        name: str,
        initial_cash: Decimal,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:accounts:write")
        row = self.tenant_store.create_account(
            identity.principal,
            name=name,
            initial_cash=initial_cash,
            idempotency_key=f"api:{idempotency_key}",
        )
        return self.get_account(identity, UUID(str(row["account_id"])))

    def fork_account(
        self,
        identity: ApiIdentity,
        *,
        parent_account_id: UUID,
        name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:accounts:write")
        row = self.tenant_store.fork_account(
            identity.principal,
            parent_account_id=parent_account_id,
            name=name,
            idempotency_key=f"api:{idempotency_key}",
        )
        return self.get_account(identity, UUID(str(row["account_id"])))

    def submit_order(
        self,
        identity: ApiIdentity,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        account_id = UUID(str(payload["account_id"]))
        strategy_id = UUID(str(payload["strategy_id"]))
        deployment_id = (
            UUID(str(payload["deployment_id"]))
            if payload.get("deployment_id")
            else None
        )
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT account.ledger_strategy_id,account.status,
                       strategy.strategy_id,strategy.status AS strategy_status
                FROM quant.paper_account_registry account
                JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=account.tenant_id
                 AND strategy.account_id=account.account_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND strategy.strategy_id=%s
                """,
                (identity.principal.tenant_id, account_id, strategy_id),
            )
            ownership = cur.fetchone()
            if ownership is None:
                raise PaperApiError(
                    "PAPER_OWNERSHIP_NOT_FOUND",
                    "account/strategy ownership chain was not found",
                    status_code=404,
                )
            if (
                ownership["status"] != "ACTIVE"
                or ownership["strategy_status"] != "ACTIVE"
            ):
                raise PaperApiError(
                    "PAPER_ACCOUNT_FROZEN",
                    "paper account or strategy is not active",
                    status_code=409,
                )
            ledger_strategy_id = str(ownership["ledger_strategy_id"])
            cur.execute(
                """
                SELECT count(*) AS count FROM quant.paper_live_order_intents
                WHERE strategy_id=%s
                  AND status IN ('QUEUED','PROCESSING','WORKING')
                """,
                (ledger_strategy_id,),
            )
            open_orders = Decimal(str(cur.fetchone()["count"]))
            cur.execute(
                """
                SELECT quantity
                FROM quant.paper_positions
                WHERE strategy_id=%s AND asset_id=%s
                """,
                (ledger_strategy_id, str(payload["asset_id"])),
            )
            position_row = cur.fetchone()
            exposure_before = Decimal(
                str(position_row["quantity"] if position_row is not None else 0)
            )
            conn.commit()
        side = str(payload["side"]).upper()
        amount_unit = str(payload.get("amount_unit") or "SHARES").upper()
        requested = Decimal(str(payload["size"]))
        limit_price = Decimal(str(payload["limit_price"]))
        requested_shares = (
            requested
            if amount_unit == "SHARES"
            else requested / limit_price
            if limit_price > 0
            else Decimal(0)
        )
        order_notional = (
            requested if amount_unit == "QUOTE" else requested_shares * limit_price
        )
        exposure_after = (
            exposure_before + requested_shares
            if side == "BUY"
            else exposure_before - requested_shares
        )
        self.ensure_retail_schema()
        retail_risk = self.retail_service.evaluate_order_risk(
            identity.principal,
            ledger_strategy_id=ledger_strategy_id,
            asset_id=str(payload["asset_id"]),
            side=side,
            limit_price=limit_price,
            requested_shares=requested_shares,
            order_notional=order_notional,
            time_in_force=str(payload["time_in_force"]).upper(),
            post_only=bool(payload.get("post_only", False)),
        )
        self.retail_service.record_order_risk_decision(
            identity.principal,
            request_key=idempotency_key,
            asset_id=str(payload["asset_id"]),
            side=side,
            decision=retail_risk,
        )
        if not retail_risk["allowed"]:
            raise PaperApiError(
                "PAPER_USER_RISK_REJECTED",
                "order was rejected by the paper wallet risk profile",
                status_code=409,
                details=retail_risk,
            )
        admission_service = self.unified_admission_service
        if self.unified_admission_enforce and admission_service is None:
            raise PaperApiError(
                "PAPER_ELIGIBILITY_UNAVAILABLE",
                "unified admission is enforced but unavailable",
                status_code=503,
            )
        if admission_service is not None and (
            self.unified_admission_shadow or self.unified_admission_enforce
        ):
            admission = admission_service.decide(
                AdmissionRequest(
                    request_id=(
                        f"paper-api-order:{identity.principal.tenant_id}:"
                        f"{idempotency_key}"
                    ),
                    operation=AdmissionOperation.ORDER,
                    account_id=str(account_id),
                    strategy_id=str(strategy_id),
                    asset_id=str(payload["asset_id"]),
                    exposure_effect=order_exposure_effect(side),
                    exposure_before=exposure_before,
                    exposure_after=exposure_after,
                    observed_at=datetime.now(timezone.utc),
                    metadata={
                        "source": "paper_public_api",
                        "amount_unit": amount_unit,
                        "post_only": bool(payload.get("post_only", False)),
                        "time_in_force": str(payload["time_in_force"]).upper(),
                    },
                )
            )
            if self.unified_admission_enforce and not admission.allowed:
                raise PaperApiError(
                    "PAPER_ELIGIBILITY_REJECTED",
                    "order is not eligible on the configured write route",
                    status_code=403,
                    details=admission.as_dict(),
                )
        self.tenant_store.check_gauge_quota(
            identity.principal,
            metric=QuotaMetric.ACCOUNT_OPEN_ORDERS,
            subject_type="ACCOUNT",
            subject_id=str(account_id),
            current_value=open_orders,
            requested=Decimal(1),
        )
        self.tenant_store.require_quota(
            identity.principal,
            metric=QuotaMetric.USER_INTENTS,
            subject_type="USER",
            subject_id=str(identity.principal.subject_user_id),
            amount=Decimal(1),
            idempotency_key=f"api-order:{idempotency_key}",
        )
        client_order_id = str(payload.get("client_order_id") or "").strip()
        if not client_order_id:
            client_order_id = (
                f"api-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]}"
            )
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        intent_id = self.live_store.submit(
            strategy_id=ledger_strategy_id,
            client_order_id=client_order_id,
            asset_id=str(payload["asset_id"]),
            side=side,
            time_in_force=str(payload["time_in_force"]).upper(),
            limit_price=limit_price,
            size=requested,
            amount_unit=amount_unit,
            post_only=bool(payload.get("post_only", False)),
            builder_code=(
                str(payload["builder_code"])
                if payload.get("builder_code") is not None
                else None
            ),
            builder_taker_fee_bps=int(payload.get("builder_taker_fee_bps") or 0),
            builder_maker_fee_bps=int(payload.get("builder_maker_fee_bps") or 0),
            decision_ts=datetime.now(timezone.utc),
            expires_at=expires_at,
            initial_status="PENDING_OWNERSHIP",
        )
        self.tenant_store.bind_intent_ownership(
            identity.principal,
            intent_id=intent_id,
            account_id=account_id,
            strategy_id=strategy_id,
            deployment_id=deployment_id,
        )
        self.retail_service.record_order_risk_decision(
            identity.principal,
            request_key=idempotency_key,
            asset_id=str(payload["asset_id"]),
            side=side,
            decision=retail_risk,
            intent_id=intent_id,
        )
        return self.get_order(identity, intent_id)

    def get_order(self, identity: ApiIdentity, intent_id: int) -> dict[str, Any]:
        identity.require_scope("paper:read")
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT ownership.account_id,ownership.strategy_id AS product_strategy_id,
                       ownership.deployment_id,intent.*
                FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s AND ownership.intent_id=%s
                """,
                (identity.principal.tenant_id, int(intent_id)),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise PaperApiError(
                "PAPER_ORDER_NOT_FOUND",
                "paper order was not found",
                status_code=404,
            )
        return dict(row)

    def list_orders(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        decoded = decode_cursor(cursor)
        cursor_id = int(decoded["intent_id"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT ownership.account_id,
                       ownership.strategy_id AS product_strategy_id,
                       ownership.deployment_id,intent.*
                FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s AND ownership.account_id=%s
                  AND (%s::bigint IS NULL OR intent.intent_id<%s)
                ORDER BY intent.intent_id DESC LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_id,
                    cursor_id,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor({"intent_id": int(items[-1]["intent_id"])})
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def cancel_order(self, identity: ApiIdentity, intent_id: int) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        order = self.get_order(identity, intent_id)
        self._record_cancel_admissions(
            identity,
            (order,),
            action="single_cancel",
        )
        changed = self.live_store.cancel(intent_id, reason="public_api_cancel")
        if not changed:
            order = self.get_order(identity, intent_id)
            if str(order["status"]) not in {
                "CANCELED",
                "COMPLETED",
                "REJECTED",
                "EXPIRED",
            }:
                raise PaperApiError(
                    "PAPER_ORDER_NOT_CANCELABLE",
                    "paper order cannot be canceled in its current state",
                    status_code=409,
                )
        return self.get_order(identity, intent_id)

    def cancel_orders(
        self,
        identity: ApiIdentity,
        intent_ids: list[int],
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        selected = sorted({int(intent_id) for intent_id in intent_ids})
        if not selected:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "at least one order_id is required",
                status_code=400,
            )
        if len(selected) > 3_000:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "at most 3000 order_ids may be canceled at once",
                status_code=400,
            )
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT ownership.account_id,
                       ownership.strategy_id AS product_strategy_id,
                       intent.intent_id,intent.status,intent.order_state,
                       intent.cancel_request_ts,intent.asset_id,
                       intent.condition_id,intent.market_id
                FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s
                  AND intent.intent_id=ANY(%s::bigint[])
                """,
                (identity.principal.tenant_id, selected),
            )
            rows = {int(row["intent_id"]): dict(row) for row in cur.fetchall()}
            conn.commit()
        self._record_cancel_admissions(
            identity,
            tuple(rows.values()),
            action="bulk_cancel",
        )
        changed = set(
            self.live_store.cancel_many(rows, reason="public_api_bulk_cancel")
        )
        canceled: list[int] = []
        not_canceled: dict[str, str] = {}
        for intent_id in selected:
            row = rows.get(intent_id)
            if row is None:
                not_canceled[str(intent_id)] = "not_found_or_not_owned"
            elif intent_id in changed or row["cancel_request_ts"] is not None or str(
                row["status"]
            ) == "CANCELED":
                canceled.append(intent_id)
            else:
                not_canceled[str(intent_id)] = (
                    f"not_cancelable:{str(row['status']).lower()}"
                )
        return {"canceled": canceled, "not_canceled": not_canceled}

    def cancel_market_orders(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        asset_id: str | None = None,
        condition_id: str | None = None,
    ) -> dict[str, Any]:
        if not str(asset_id or "").strip() and not str(condition_id or "").strip():
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "asset_id or condition_id is required",
                status_code=400,
            )
        return self._cancel_account_orders(
            identity,
            account_id=account_id,
            asset_id=str(asset_id).strip() if asset_id else None,
            condition_id=str(condition_id).strip() if condition_id else None,
            reason="public_api_market_cancel",
        )

    def cancel_all_orders(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
    ) -> dict[str, Any]:
        return self._cancel_account_orders(
            identity,
            account_id=account_id,
            reason="public_api_cancel_all",
        )

    def _cancel_account_orders(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        reason: str,
        asset_id: str | None = None,
        condition_id: str | None = None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT account_id
                FROM quant.paper_account_registry
                WHERE tenant_id=%s AND account_id=%s
                """,
                (identity.principal.tenant_id, account_id),
            )
            if cur.fetchone() is None:
                raise PaperApiError(
                    "PAPER_ACCOUNT_NOT_FOUND",
                    "paper account was not found",
                    status_code=404,
                )
            cur.execute(
                """
                SELECT ownership.account_id,
                       ownership.strategy_id AS product_strategy_id,
                       intent.intent_id,intent.asset_id,
                       intent.condition_id,intent.market_id
                FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s
                  AND ownership.account_id=%s
                  AND (%s::text IS NULL OR intent.asset_id=%s)
                  AND (%s::text IS NULL OR intent.condition_id=%s)
                  AND intent.status IN ('QUEUED','PROCESSING','WORKING')
                ORDER BY intent.intent_id
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    asset_id,
                    asset_id,
                    condition_id,
                    condition_id,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            selected = [int(row["intent_id"]) for row in rows]
            conn.commit()
        self._record_cancel_admissions(
            identity,
            tuple(rows),
            action=reason,
        )
        canceled = self.live_store.cancel_many(selected, reason=reason)
        return {
            "account_id": account_id,
            "asset_id": asset_id,
            "condition_id": condition_id,
            "matched": len(selected),
            "canceled": canceled,
            "not_canceled": len(selected) - len(canceled),
        }

    def replace_order(
        self,
        identity: ApiIdentity,
        intent_id: int,
        *,
        limit_price: Decimal,
        size: Decimal,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        order = self.get_order(identity, intent_id)
        self._require_replace_admission(
            identity,
            order=order,
            limit_price=Decimal(limit_price),
            size=Decimal(size),
        )
        changed = self.live_store.replace(
            intent_id,
            limit_price=limit_price,
            size=size,
            reason="public_api_replace",
        )
        if not changed:
            raise PaperApiError(
                "PAPER_ORDER_NOT_REPLACEABLE",
                "paper order cannot be replaced in its current state",
                status_code=409,
            )
        return self.get_order(identity, intent_id)

    def _record_cancel_admissions(
        self,
        identity: ApiIdentity,
        orders: Sequence[Mapping[str, Any]],
        *,
        action: str,
    ) -> None:
        service = self.unified_admission_service
        if service is None:
            return
        observed_at = datetime.now(timezone.utc)
        for order in orders:
            intent_id = int(order["intent_id"])
            decision = service.decide(
                AdmissionRequest(
                    request_id=(
                        f"paper-api-cancel:{identity.principal.tenant_id}:"
                        f"{intent_id}:{action}:{observed_at.isoformat()}"
                    ),
                    operation=AdmissionOperation.ORDER_CANCEL,
                    account_id=str(
                        order.get("account_id") or identity.principal.tenant_id
                    ),
                    strategy_id=(
                        str(order["product_strategy_id"])
                        if order.get("product_strategy_id") is not None
                        else None
                    ),
                    asset_id=(
                        str(order["asset_id"])
                        if order.get("asset_id") is not None
                        else None
                    ),
                    condition_id=(
                        str(order["condition_id"])
                        if order.get("condition_id") is not None
                        else None
                    ),
                    market_id=(
                        str(order["market_id"])
                        if order.get("market_id") is not None
                        else None
                    ),
                    exposure_effect=ExposureEffect.NEUTRAL,
                    observed_at=observed_at,
                    metadata={"source": "paper_public_api", "action": action},
                )
            )
            if not decision.allowed:
                raise PaperApiError(
                    "PAPER_CANCEL_ADMISSION_ERROR",
                    "a risk-reducing cancel was unexpectedly denied",
                    status_code=503,
                )

    def _require_replace_admission(
        self,
        identity: ApiIdentity,
        *,
        order: Mapping[str, Any],
        limit_price: Decimal,
        size: Decimal,
    ) -> None:
        service = self.unified_admission_service
        if self.unified_admission_enforce and service is None:
            raise PaperApiError(
                "PAPER_ELIGIBILITY_UNAVAILABLE",
                "unified admission is enforced but unavailable",
                status_code=503,
            )
        if service is None or not (
            self.unified_admission_shadow or self.unified_admission_enforce
        ):
            return
        side = str(order["side"]).upper()
        amount_unit = str(order.get("amount_unit") or "SHARES").upper()
        shares = size if amount_unit == "SHARES" else (
            size / limit_price if limit_price > 0 else Decimal(0)
        )
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM quant.paper_positions "
                "WHERE strategy_id=%s AND asset_id=%s",
                (str(order["strategy_id"]), str(order["asset_id"])),
            )
            position = cur.fetchone()
        before = Decimal(str(position["quantity"] if position is not None else 0))
        after = before + shares if side == "BUY" else before - shares
        observed_at = datetime.now(timezone.utc)
        decision = service.decide(
            AdmissionRequest(
                request_id=(
                    f"paper-api-replace:{identity.principal.tenant_id}:"
                    f"{int(order['intent_id'])}:{observed_at.isoformat()}"
                ),
                operation=AdmissionOperation.ORDER,
                account_id=str(order["account_id"]),
                strategy_id=str(order.get("product_strategy_id") or order["strategy_id"]),
                asset_id=str(order["asset_id"]),
                condition_id=(
                    str(order["condition_id"])
                    if order.get("condition_id") is not None
                    else None
                ),
                market_id=(
                    str(order["market_id"])
                    if order.get("market_id") is not None
                    else None
                ),
                exposure_effect=order_exposure_effect(side),
                exposure_before=before,
                exposure_after=after,
                observed_at=observed_at,
                metadata={"source": "paper_public_api", "action": "replace"},
            )
        )
        if self.unified_admission_enforce and not decision.allowed:
            raise PaperApiError(
                "PAPER_ELIGIBILITY_REJECTED",
                "paper order replacement was rejected by unified admission: "
                + ",".join(decision.reason_codes),
                status_code=403,
            )

    def list_positions(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        account = self.get_account(identity, account_id)
        decoded = decode_cursor(cursor)
        cursor_asset = str(decoded["asset_id"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT position.*,
                       registry.market_slug,registry.market_title,
                       registry.outcome_name,registry.market_state,
                       mark.observed_at AS mark_observed_at,
                       mark.research_mark,mark.liquidation_mark,
                       mark.conservative_mark,mark.mark_quality,
                       mark.mark_age_ms,mark.best_bid,mark.best_ask,
                       mark.checkpoint_id AS mark_checkpoint_id
                FROM quant.paper_account_registry account
                JOIN quant.paper_positions position
                  ON position.strategy_id=account.ledger_strategy_id
                LEFT JOIN quant.paper_position_marks mark
                  ON mark.strategy_id=position.strategy_id
                 AND mark.asset_id=position.asset_id
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=position.asset_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (%s::text IS NULL OR position.asset_id>%s)
                ORDER BY position.asset_id LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_asset,
                    cursor_asset,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            asset_ids = [str(row["asset_id"]) for row in rows]
            ledger_rows: list[dict[str, Any]] = []
            if asset_ids:
                cur.execute(
                    """
                    SELECT asset_id,event_type,shares_delta,fee,position_after,
                           cost_basis_after,entry_id
                    FROM quant.paper_ledger_entries
                    WHERE strategy_id=%s AND asset_id=ANY(%s::text[])
                    ORDER BY event_ts,entry_id
                    """,
                    (str(account["ledger_strategy_id"]), asset_ids),
                )
                ledger_rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        ledger_by_asset: dict[str, list[dict[str, Any]]] = {}
        for ledger_row in ledger_rows:
            ledger_by_asset.setdefault(str(ledger_row["asset_id"]), []).append(
                ledger_row
            )
        for item in items:
            item["economics"] = reconstruct_position_economics(
                ledger_by_asset.get(str(item["asset_id"]), []),
                current_quantity=Decimal(str(item["quantity"])),
                current_gross_basis=Decimal(str(item["cost_basis"])),
            )
        next_cursor = (
            encode_cursor({"asset_id": str(items[-1]["asset_id"])})
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_performance(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """Return current account equity plus an auditable NAV history page."""

        identity.require_scope("paper:read")
        account = self.get_account(identity, account_id)
        decoded = decode_cursor(cursor)
        cursor_observed = str(decoded["observed_at"]) if decoded else None
        cursor_nav_id = int(decoded["nav_id"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT nav.*,risk.trading_enabled,risk.kill_switch,
                       risk.reason AS risk_lock_reason,
                       (
                         SELECT count(*) FROM quant.paper_live_order_intents intent
                         WHERE intent.strategy_id=account.ledger_strategy_id
                           AND intent.status NOT IN
                               ('CANCELED','COMPLETED','REJECTED','EXPIRED')
                       ) AS open_orders
                FROM quant.paper_account_registry account
                LEFT JOIN quant.paper_portfolio_nav_current nav
                  ON nav.strategy_id=account.ledger_strategy_id
                LEFT JOIN quant.paper_strategy_risk_controls risk
                  ON risk.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                """,
                (identity.principal.tenant_id, account_id),
            )
            current_row = cur.fetchone()
            cur.execute(
                """
                SELECT nav.* FROM quant.paper_account_registry account
                JOIN quant.paper_portfolio_nav_snapshots nav
                  ON nav.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (
                    %s::timestamptz IS NULL
                    OR (nav.observed_at,nav.nav_id)
                       < (%s::timestamptz,%s::bigint)
                  )
                ORDER BY nav.observed_at DESC,nav.nav_id DESC
                LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_observed,
                    cursor_observed,
                    cursor_nav_id,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        current = dict(current_row) if current_row is not None else {}
        has_current_nav = isinstance(current.get("observed_at"), datetime)
        has_more = len(rows) > limit
        history = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "observed_at": history[-1]["observed_at"].isoformat(),
                    "nav_id": int(history[-1]["nav_id"]),
                }
            )
            if has_more and history
            else None
        )
        metadata = current.get("metadata") or {}
        views = metadata.get("valuation_views") if isinstance(metadata, dict) else {}
        views = views if isinstance(views, dict) else {}
        cash_balance = Decimal(str(account["cash_balance"]))
        cash_reserved = Decimal(str(account["cash_reserved"]))
        nav_complete = has_current_nav and bool(current.get("nav_complete", False))
        account_status = str(account["status"])
        if bool(current.get("kill_switch")) or current.get("trading_enabled") is False:
            effective_status = "RISK_LOCKED"
        elif current.get("equity") is not None and Decimal(str(current["equity"])) < 0:
            effective_status = "INSOLVENT"
        elif has_current_nav and not nav_complete:
            effective_status = "DATA_DEGRADED"
        else:
            effective_status = account_status
        pnl_rows = list(reversed(history))
        if has_current_nav and (
            not pnl_rows
            or pnl_rows[-1].get("observed_at") != current.get("observed_at")
        ):
            pnl_rows.append(current)
        professional_pnl = build_professional_pnl_report(
            [nav_point_from_snapshot(row) for row in pnl_rows],
            initial_nav=Decimal(str(account["initial_cash"])),
        )
        professional_pnl["quality"]["window_truncated"] = next_cursor is not None
        return {
            "account": account,
            "summary": {
                "balance": cash_balance,
                "equity": (
                    current.get("equity")
                    if current.get("equity") is not None
                    else cash_balance
                ),
                "available_cash": cash_balance - cash_reserved,
                "reserved_cash": cash_reserved,
                "realized_pnl": account["realized_pnl"],
                "unrealized_pnl": current.get("unrealized_pnl"),
                "confirmed_nav": views.get("confirmed_nav"),
                "liquidation_nav": views.get(
                    "walk_book_liquidation_nav",
                    current.get("conservative_equity"),
                ),
                "open_orders": int(current.get("open_orders") or 0),
                "open_positions": int(current.get("open_positions") or 0),
                "effective_status": effective_status,
                "risk_lock_reason": current.get("risk_lock_reason"),
            },
            "current": current or None,
            "history": history,
            "professional_pnl": professional_pnl,
            "next_cursor": next_cursor,
            "data_quality": {
                "nav_complete": nav_complete,
                "nav_status": "AVAILABLE" if has_current_nav else "NO_SNAPSHOT",
                "unmarkable_positions": int(current.get("unmarkable_positions") or 0),
                "observed_at": current.get("observed_at"),
                "valuation_model_version": views.get("valuation_model_version"),
            },
        }

    def list_journal(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        self.get_account(identity, account_id)
        decoded = decode_cursor(cursor)
        cursor_event = str(decoded["event_ts"]) if decoded else None
        cursor_journal = str(decoded["journal_id"]) if decoded else None
        cursor_line = int(decoded["line_index"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT line.* FROM quant.paper_account_registry account
                JOIN quant.paper_journal_lines line
                  ON line.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (
                    %s::timestamptz IS NULL
                    OR (line.event_ts,line.journal_id,line.line_index)
                       < (%s::timestamptz,%s::text,%s::integer)
                  )
                ORDER BY line.event_ts DESC,line.journal_id DESC,line.line_index DESC
                LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_event,
                    cursor_event,
                    cursor_journal,
                    cursor_line,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "event_ts": items[-1]["event_ts"].isoformat(),
                    "journal_id": str(items[-1]["journal_id"]),
                    "line_index": int(items[-1]["line_index"]),
                }
            )
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def list_tca(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        self.get_account(identity, account_id)
        decoded = decode_cursor(cursor)
        cursor_updated = str(decoded["updated_at"]) if decoded else None
        cursor_order = str(decoded["order_id"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT tca.* FROM quant.paper_account_registry account
                JOIN quant.execution_tca tca
                  ON tca.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (
                    %s::timestamptz IS NULL
                    OR (tca.updated_at,tca.order_id)
                       < (%s::timestamptz,%s::text)
                  )
                ORDER BY tca.updated_at DESC,tca.order_id DESC
                LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_updated,
                    cursor_updated,
                    cursor_order,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "updated_at": items[-1]["updated_at"].isoformat(),
                    "order_id": str(items[-1]["order_id"]),
                }
            )
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_order_audit(
        self,
        identity: ApiIdentity,
        intent_id: int,
    ) -> dict[str, Any]:
        """Build a tenant-scoped order timeline from persisted source evidence."""

        identity.require_scope("paper:read")
        order = self.get_order(identity, intent_id)
        tenant_id = identity.principal.tenant_id
        account_id = UUID(str(order["account_id"]))
        audit_key = str(order.get("result_audit_key") or "")
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT event.* FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_order_events event
                  ON event.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s AND ownership.intent_id=%s
                ORDER BY event.event_ts,event.event_id
                """,
                (tenant_id, intent_id),
            )
            events = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT risk.* FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_risk_decisions risk
                  ON risk.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s AND ownership.intent_id=%s
                """,
                (tenant_id, intent_id),
            )
            risk_row = cur.fetchone()
            cur.execute(
                """
                SELECT audit.* FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                LEFT JOIN quant.paper_taker_order_audits audit
                  ON audit.audit_key=intent.result_audit_key
                  OR (
                    audit.strategy_id=intent.strategy_id
                    AND audit.client_order_id=intent.client_order_id
                  )
                WHERE ownership.tenant_id=%s AND ownership.intent_id=%s
                ORDER BY audit.created_at DESC NULLS LAST LIMIT 1
                """,
                (tenant_id, intent_id),
            )
            audit_row = cur.fetchone()
            cur.execute(
                """
                SELECT tca.* FROM quant.paper_intent_ownership ownership
                LEFT JOIN quant.execution_tca tca
                  ON tca.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s AND ownership.intent_id=%s
                ORDER BY tca.updated_at DESC NULLS LAST LIMIT 1
                """,
                (tenant_id, intent_id),
            )
            tca_row = cur.fetchone()
            cur.execute(
                """
                SELECT fill.* FROM quant.paper_account_registry account
                JOIN quant.paper_fills fill
                  ON fill.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (%s::text<>'' AND fill.audit_key=%s)
                ORDER BY fill.fill_index
                """,
                (tenant_id, account_id, audit_key, audit_key),
            )
            fills = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT ledger.* FROM quant.paper_account_registry account
                JOIN quant.paper_ledger_entries ledger
                  ON ledger.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (%s::text<>'' AND ledger.audit_key=%s)
                ORDER BY ledger.event_ts,ledger.entry_id
                """,
                (tenant_id, account_id, audit_key, audit_key),
            )
            ledger = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT queue.* FROM quant.paper_intent_ownership ownership
                LEFT JOIN quant.maker_queue_states queue
                  ON queue.paper_order_id=ownership.intent_id::text
                WHERE ownership.tenant_id=%s AND ownership.intent_id=%s
                """,
                (tenant_id, intent_id),
            )
            queue_row = cur.fetchone()
            conn.commit()
        audit = dict(audit_row) if audit_row and audit_row.get("audit_key") else None
        tca = dict(tca_row) if tca_row and tca_row.get("order_id") else None
        risk = dict(risk_row) if risk_row else None
        fidelity = dict((audit or {}).get("fidelity") or {})
        coverage_grade = (audit or {}).get("coverage_grade")
        quality = {
            "execution_fidelity": fidelity.get("fidelity_level")
            or (tca or {}).get("fidelity_level")
            or "UNASSESSED",
            "model_confidence": fidelity.get("model_confidence")
            or fidelity.get("confidence")
            or "UNASSESSED",
            "calibration_domain": fidelity.get("calibration_domain") or "UNASSESSED",
            "data_quality": (
                "DATA_DEGRADED"
                if coverage_grade not in {None, "A", "B"}
                else f"COVERAGE_{coverage_grade}"
                if coverage_grade
                else "UNASSESSED"
            ),
            "coverage_grade": coverage_grade,
            "book_age_ms": (audit or {}).get("book_age_ms"),
            "capacity_status": (tca or {}).get("capacity_status")
            or ((risk or {}).get("metrics") or {}).get("capacity_status")
            or "NOT_EVALUATED",
            "model_version": (audit or {}).get("model_version")
            or (tca or {}).get("model_version")
            or "UNBOUND",
        }
        timeline = [
            {
                "kind": "ORDER_EVENT",
                "state": event["to_state"],
                "event_type": event["event_type"],
                "reason": event.get("reason"),
                "event_ts": event["event_ts"],
                "checkpoint_id": event.get("checkpoint_id"),
                "payload": event.get("payload") or {},
            }
            for event in events
        ]
        timeline.extend(
            {
                "kind": "LEDGER",
                "state": row["event_type"],
                "event_type": row["event_type"],
                "reason": None,
                "event_ts": row["event_ts"],
                "checkpoint_id": None,
                "payload": {
                    "cash_delta": row["cash_delta"],
                    "shares_delta": row["shares_delta"],
                    "realized_pnl_delta": row["realized_pnl_delta"],
                },
            }
            for row in ledger
        )
        timeline.sort(key=lambda row: (row["event_ts"], row["kind"]))
        return {
            "order": order,
            "timeline": timeline,
            "risk": risk,
            "execution_audit": audit,
            "fills": fills,
            "ledger": ledger,
            "tca": tca,
            "maker_queue": dict(queue_row)
            if queue_row and queue_row.get("paper_order_id")
            else None,
            "quality": quality,
        }

    def export_account(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        resource: str,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return one bounded export set; serialization and byte quota live at HTTP edge."""

        identity.require_scope("paper:read")
        selected = str(resource).strip().lower()
        allowed = {
            "orders": (
                """SELECT intent.* FROM quant.paper_account_registry account
                    JOIN quant.paper_live_order_intents intent
                      ON intent.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY intent.intent_id DESC LIMIT %s"""
            ),
            "positions": (
                """SELECT position.* FROM quant.paper_account_registry account
                    JOIN quant.paper_positions position
                      ON position.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY position.asset_id LIMIT %s"""
            ),
            "fills": (
                """SELECT fill.* FROM quant.paper_account_registry account
                    JOIN quant.paper_fills fill
                      ON fill.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY fill.created_at DESC,fill.audit_key DESC,fill.fill_index DESC
                    LIMIT %s"""
            ),
            "ledger": (
                """SELECT ledger.* FROM quant.paper_account_registry account
                    JOIN quant.paper_ledger_entries ledger
                      ON ledger.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY ledger.event_ts DESC,ledger.entry_id DESC LIMIT %s"""
            ),
            "journal": (
                """SELECT line.* FROM quant.paper_account_registry account
                    JOIN quant.paper_journal_lines line
                      ON line.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY line.event_ts DESC,line.journal_id DESC,line.line_index DESC
                    LIMIT %s"""
            ),
            "nav": (
                """SELECT nav.* FROM quant.paper_account_registry account
                    JOIN quant.paper_portfolio_nav_snapshots nav
                      ON nav.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY nav.observed_at DESC,nav.nav_id DESC LIMIT %s"""
            ),
            "tca": (
                """SELECT tca.* FROM quant.paper_account_registry account
                    JOIN quant.execution_tca tca
                      ON tca.strategy_id=account.ledger_strategy_id
                    WHERE account.tenant_id=%s AND account.account_id=%s
                    ORDER BY tca.updated_at DESC,tca.order_id DESC LIMIT %s"""
            ),
        }
        if selected not in allowed:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "resource must be orders, positions, fills, ledger, journal, nav, or tca",
                status_code=400,
            )
        self.get_account(identity, account_id)
        bounded_limit = max(1, min(int(limit), 10_000))
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                allowed[selected],
                (identity.principal.tenant_id, account_id, bounded_limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return rows

    def charge_export_quota(
        self,
        identity: ApiIdentity,
        *,
        byte_count: int,
        request_id: UUID,
    ) -> Any:
        return self.tenant_store.require_quota(
            identity.principal,
            metric=QuotaMetric.TENANT_ARCHIVE_EXPORT_BYTES,
            subject_type="TENANT",
            subject_id=str(identity.principal.tenant_id),
            amount=Decimal(max(0, int(byte_count))),
            idempotency_key=f"account-export:{request_id}",
            permission=PaperPermission.ACCOUNT_READ,
        )

    def list_fills(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        self.get_account(identity, account_id)
        decoded = decode_cursor(cursor)
        cursor_created = str(decoded["created_at"]) if decoded else None
        cursor_audit = str(decoded["audit_key"]) if decoded else None
        cursor_index = int(decoded["fill_index"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT fill.* FROM quant.paper_account_registry account
                JOIN quant.paper_fills fill
                  ON fill.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (
                    %s::timestamptz IS NULL
                    OR (fill.created_at,fill.audit_key,fill.fill_index)
                       < (%s::timestamptz,%s::text,%s::integer)
                  )
                ORDER BY fill.created_at DESC,fill.audit_key DESC,fill.fill_index DESC
                LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_created,
                    cursor_created,
                    cursor_audit,
                    cursor_index,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "created_at": items[-1]["created_at"].isoformat(),
                    "audit_key": str(items[-1]["audit_key"]),
                    "fill_index": int(items[-1]["fill_index"]),
                }
            )
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def list_ledger(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        self.get_account(identity, account_id)
        decoded = decode_cursor(cursor)
        cursor_event = str(decoded["event_ts"]) if decoded else None
        cursor_entry = int(decoded["entry_id"]) if decoded else None
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT ledger.* FROM quant.paper_account_registry account
                JOIN quant.paper_ledger_entries ledger
                  ON ledger.strategy_id=account.ledger_strategy_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (
                    %s::timestamptz IS NULL
                    OR (ledger.event_ts,ledger.entry_id)
                       < (%s::timestamptz,%s::bigint)
                  )
                ORDER BY ledger.event_ts DESC,ledger.entry_id DESC
                LIMIT %s
                """,
                (
                    identity.principal.tenant_id,
                    account_id,
                    cursor_event,
                    cursor_event,
                    cursor_entry,
                    limit + 1,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "event_ts": items[-1]["event_ts"].isoformat(),
                    "entry_id": int(items[-1]["entry_id"]),
                }
            )
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def create_replay_session(
        self,
        identity: ApiIdentity,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        strategy_id = (
            UUID(str(payload["strategy_id"])) if payload.get("strategy_id") else None
        )
        return self.replay_service.create_session(
            identity.principal,
            account_id=UUID(str(payload["account_id"])),
            strategy_id=strategy_id,
            name=str(payload.get("name") or "").strip(),
            start_ts=parse_replay_timestamp(payload.get("start_ts"), field="start_ts"),
            end_ts=parse_replay_timestamp(payload.get("end_ts"), field="end_ts"),
            speed=Decimal(str(payload.get("speed") or "1")),
            seed=int(payload.get("seed") or 0),
            strategy_version=str(payload.get("strategy_version") or "").strip(),
            execution_model=str(payload.get("execution_model") or "").strip(),
            benchmark=str(payload.get("benchmark") or "CASH"),
            idempotency_key=f"api:{idempotency_key}",
        )

    def list_replay_sessions(
        self,
        identity: ApiIdentity,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        decoded = decode_cursor(cursor)
        cursor_created = (
            parse_replay_timestamp(decoded["created_at"], field="cursor.created_at")
            if decoded
            else None
        )
        cursor_session = UUID(str(decoded["replay_session_id"])) if decoded else None
        rows = self.replay_service.list_sessions(
            identity.principal,
            limit=limit + 1,
            cursor_created_at=cursor_created,
            cursor_session_id=cursor_session,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "created_at": items[-1]["created_at"],
                    "replay_session_id": items[-1]["replay_session_id"],
                }
            )
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_replay_session(
        self, identity: ApiIdentity, replay_session_id: UUID
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        return self.replay_service.get_session(identity.principal, replay_session_id)

    def pause_replay_session(
        self, identity: ApiIdentity, replay_session_id: UUID
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        return self.replay_service.pause_session(identity.principal, replay_session_id)

    def resume_replay_session(
        self,
        identity: ApiIdentity,
        replay_session_id: UUID,
        *,
        max_events: int,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        return self.replay_service.resume_session(
            identity.principal, replay_session_id, max_events=max_events
        )

    def fork_replay_session(
        self,
        identity: ApiIdentity,
        replay_session_id: UUID,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        return self.replay_service.fork_session(
            identity.principal,
            replay_session_id,
            name=str(payload.get("name") or "").strip(),
            speed=(
                Decimal(str(payload["speed"]))
                if payload.get("speed") not in (None, "")
                else None
            ),
            seed=(
                int(payload["seed"]) if payload.get("seed") not in (None, "") else None
            ),
            execution_model=(
                str(payload["execution_model"]).strip()
                if payload.get("execution_model")
                else None
            ),
            benchmark=(
                str(payload["benchmark"]).strip() if payload.get("benchmark") else None
            ),
            idempotency_key=f"api:{idempotency_key}",
        )

    def list_replay_events(
        self,
        identity: ApiIdentity,
        replay_session_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        decoded = decode_cursor(cursor)
        start_index = int(decoded["event_index"]) if decoded else 0
        rows = self.replay_service.list_events(
            identity.principal,
            replay_session_id,
            start_index=start_index,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor({"event_index": int(items[-1]["event_index"]) + 1})
            if has_more and items
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def get_replay_report(
        self, identity: ApiIdentity, replay_session_id: UUID
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        return self.replay_service.get_report(identity.principal, replay_session_id)

    def create_scenario_run(
        self,
        identity: ApiIdentity,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        return self.scenario_service.create_run(
            identity.principal,
            account_id=UUID(str(payload["account_id"])),
            strategy_id=(
                UUID(str(payload["strategy_id"]))
                if payload.get("strategy_id")
                else None
            ),
            name=str(payload["name"]),
            inputs=dict(payload.get("inputs") or {}),
            idempotency_key=f"api:{idempotency_key}",
        )

    def list_scenario_runs(
        self, identity: ApiIdentity, *, account_id: UUID, limit: int
    ) -> list[dict[str, Any]]:
        identity.require_scope("paper:read")
        return self.scenario_service.list_runs(
            identity.principal, account_id=account_id, limit=limit
        )

    def get_scenario_run(
        self, identity: ApiIdentity, scenario_run_id: UUID
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        return self.scenario_service.get_run(identity.principal, scenario_run_id)

    def create_conditional_order(
        self,
        identity: ApiIdentity,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        return self.conditional_service.arm_order(
            identity.principal,
            account_id=UUID(str(payload["account_id"])),
            strategy_id=UUID(str(payload["strategy_id"])),
            order_type=str(payload["order_type"]),
            trigger=dict(payload["trigger"]),
            child_order=dict(payload["child_order"]),
            idempotency_key=f"api:{idempotency_key}",
            group_id=UUID(str(payload["group_id"]))
            if payload.get("group_id")
            else None,
            group_policy=str(payload.get("group_policy") or "NONE"),
            parent_conditional_order_id=(
                UUID(str(payload["parent_conditional_order_id"]))
                if payload.get("parent_conditional_order_id")
                else None
            ),
            parent_intent_id=(
                int(payload["parent_intent_id"])
                if payload.get("parent_intent_id") is not None
                else None
            ),
            expires_at=(
                parse_replay_timestamp(payload["expires_at"], field="expires_at")
                if payload.get("expires_at")
                else None
            ),
        )

    def create_conditional_order_group(
        self,
        identity: ApiIdentity,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        orders = []
        for raw in list(payload["orders"]):
            leg = dict(raw)
            if leg.get("expires_at"):
                leg["expires_at"] = parse_replay_timestamp(
                    leg["expires_at"], field="expires_at"
                )
            orders.append(leg)
        return self.conditional_service.arm_group(
            identity.principal,
            account_id=UUID(str(payload["account_id"])),
            strategy_id=UUID(str(payload["strategy_id"])),
            group_policy=str(payload["group_policy"]),
            orders=orders,
            idempotency_key=f"api:{idempotency_key}",
        )

    def list_conditional_orders(
        self, identity: ApiIdentity, *, account_id: UUID, limit: int
    ) -> list[dict[str, Any]]:
        identity.require_scope("paper:read")
        return self.conditional_service.list_orders(
            identity.principal, account_id=account_id, limit=limit
        )

    def get_conditional_order(
        self, identity: ApiIdentity, conditional_order_id: UUID
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        return self.conditional_service.get_order(
            identity.principal, conditional_order_id
        )

    def cancel_conditional_order(
        self,
        identity: ApiIdentity,
        conditional_order_id: UUID,
        *,
        reason: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:trade")
        return self.conditional_service.cancel_order(
            identity.principal, conditional_order_id, reason=reason
        )

    def list_retail_position_operations(
        self,
        identity: ApiIdentity,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return CTF-style Paper operations owned by the current wallet."""

        identity.require_scope("paper:read")
        wallet = self.retail_service.get_default_wallet(identity.principal)
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT operation.*,application.realized_pnl_delta,
                       reservation.reserved_cash,
                       reservation.status AS reservation_status,
                       registry.market_slug,registry.market_title
                FROM quant.simulator_position_operations operation
                LEFT JOIN quant.paper_position_operation_applications application
                  ON application.operation_id=operation.operation_id
                LEFT JOIN quant.simulator_position_operation_reservations reservation
                  ON reservation.operation_id=operation.operation_id
                LEFT JOIN LATERAL (
                    SELECT token.market_slug,token.market_title
                    FROM quant.paper_market_registry_tokens token
                    WHERE token.condition_id=operation.condition_id
                    ORDER BY token.updated_at DESC,token.asset_id LIMIT 1
                ) registry ON TRUE
                WHERE operation.account_id=%s AND operation.strategy_id=%s
                ORDER BY operation.created_at DESC,operation.operation_id DESC
                LIMIT %s
                """,
                (
                    str(wallet["account_id"]),
                    str(wallet["ledger_strategy_id"]),
                    int(limit),
                ),
            )
            rows = [self._serialize_position_operation_row(dict(row)) for row in cur.fetchall()]
            conn.commit()
        return {
            "items": rows,
            "redeem_mode": "AUTOMATIC_CONFIRMED_FINALITY",
            "paper_receipts_are_chain_transactions": False,
        }

    def get_retail_position_operation(
        self,
        identity: ApiIdentity,
        operation_id: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:read")
        wallet = self.retail_service.get_default_wallet(identity.principal)
        record = self.position_operation_store.operation(str(operation_id))
        if record is None or (
            record.intent.account_id != str(wallet["account_id"])
            or record.intent.strategy_id != str(wallet["ledger_strategy_id"])
        ):
            raise PaperApiError(
                "PAPER_POSITION_OPERATION_NOT_FOUND",
                "paper position operation was not found",
                status_code=404,
            )
        return self._serialize_position_operation_record(
            record,
            events=self.position_operation_store.events(str(operation_id)),
            reservation=self.position_operation_store.reservation(str(operation_id)),
        )

    def get_retail_position_operation_candidates(
        self,
        identity: ApiIdentity,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Resolve user-facing merge, redeem and neg-risk choices server-side."""

        identity.require_scope("paper:read")
        wallet = self.retail_service.get_default_wallet(identity.principal)
        strategy_id = str(wallet["ledger_strategy_id"])
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                WITH held AS (
                    SELECT position.condition_id,position.market_id,
                           position.asset_id,
                           position.quantity-position.reserved_quantity AS available,
                           registry.market_slug,registry.market_title,
                           registry.outcome_name
                    FROM quant.paper_positions position
                    LEFT JOIN quant.paper_market_registry_tokens registry
                      ON registry.asset_id=position.asset_id
                    WHERE position.strategy_id=%s
                      AND position.quantity-position.reserved_quantity>0
                )
                SELECT condition_id,market_id,max(market_slug) AS market_slug,
                       max(market_title) AS market_title,min(available) AS max_amount,
                       jsonb_agg(jsonb_build_object(
                           'asset_id',asset_id,'outcome_name',outcome_name,
                           'available',available
                       ) ORDER BY outcome_name,asset_id) AS legs
                FROM held
                GROUP BY condition_id,market_id
                HAVING count(DISTINCT asset_id)=2
                ORDER BY max(market_title),condition_id LIMIT %s
                """,
                (strategy_id, int(limit)),
            )
            merge_candidates = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT receivable.condition_id,receivable.asset_id,
                       receivable.quantity,receivable.expected_payout,
                       receivable.expected_realized_pnl,receivable.state,
                       registry.market_slug,registry.market_title,
                       registry.outcome_name
                FROM quant.paper_settlement_receivables receivable
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=receivable.asset_id
                WHERE receivable.strategy_id=%s
                  AND receivable.state IN ('ACCRUED','REDEEMABLE','REDEEMING')
                  AND receivable.cash_applied=FALSE
                ORDER BY receivable.accrued_at DESC,receivable.asset_id LIMIT %s
                """,
                (strategy_id, int(limit)),
            )
            redeemables = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT to_regclass(
                    'quant.simulator_augmented_conversion_matrices'
                ) AS matrix_table
                """
            )
            matrix_table = cur.fetchone()["matrix_table"]
            convert_candidates: list[dict[str, Any]] = []
            if matrix_table is not None:
                cur.execute(
                    """
                    SELECT matrix.matrix_id,matrix.event_version_id,
                           matrix.source_outcome_id,matrix.rule_version,
                           matrix.token_deltas_per_unit,
                           source.label AS source_label,
                           source.condition_id,source.no_asset_id,
                           position.quantity-position.reserved_quantity AS max_amount
                    FROM quant.simulator_augmented_conversion_matrices matrix
                    JOIN quant.simulator_augmented_neg_risk_events event
                      ON event.event_version_id=matrix.event_version_id
                    JOIN quant.simulator_augmented_neg_risk_outcomes source
                      ON source.event_version_id=matrix.event_version_id
                     AND source.outcome_id=matrix.source_outcome_id
                    JOIN quant.paper_positions position
                      ON position.strategy_id=%s
                     AND position.asset_id=source.no_asset_id
                    WHERE source.active AND source.tradeable
                      AND position.quantity-position.reserved_quantity>0
                      AND event.version=(
                          SELECT max(latest.version)
                          FROM quant.simulator_augmented_neg_risk_events latest
                          WHERE latest.event_key=event.event_key
                      )
                    ORDER BY event.effective_ts DESC,source.label LIMIT %s
                    """,
                    (strategy_id, int(limit)),
                )
                convert_candidates = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return {
            "split": {
                "available": True,
                "market_source": "retail_live_binary_markets",
            },
            "merge": merge_candidates,
            "neg_risk_convert": convert_candidates,
            "redeem": redeemables,
            "redeem_mode": "AUTOMATIC_CONFIRMED_FINALITY",
        }

    def create_retail_position_operation(
        self,
        identity: ApiIdentity,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create and durably finalize one chain-off Paper asset operation."""

        identity.require_scope("paper:trade")
        wallet = self.retail_service.get_default_wallet(identity.principal)
        operation_id = "paper-position-operation:" + hashlib.sha256(
            (
                f"{identity.principal.tenant_id}:"
                f"{identity.principal.subject_user_id}:{idempotency_key}"
            ).encode("utf-8")
        ).hexdigest()
        record = self.position_operation_store.operation(operation_id)
        if record is None:
            intent = self._build_retail_position_operation_intent(
                identity,
                wallet=wallet,
                operation_id=operation_id,
                payload=payload,
            )
            self._require_position_operation_admission(identity, intent)
            try:
                record = self.position_operation_store.create(
                    intent,
                    event_id=f"{operation_id}:created",
                )
            except PositionOperationReservationError as exc:
                raise PaperApiError(
                    "PAPER_POSITION_OPERATION_RESOURCES_UNAVAILABLE",
                    str(exc),
                    status_code=409,
                ) from exc
        elif (
            record.intent.account_id != str(wallet["account_id"])
            or record.intent.strategy_id != str(wallet["ledger_strategy_id"])
        ):
            raise PaperApiError(
                "PAPER_POSITION_OPERATION_IDEMPOTENCY_CONFLICT",
                "operation id belongs to another paper wallet",
                status_code=409,
            )
        record = self._advance_retail_position_operation(record.intent.event_id)
        return self._serialize_position_operation_record(
            record,
            events=self.position_operation_store.events(operation_id),
            reservation=self.position_operation_store.reservation(operation_id),
        )

    def _build_retail_position_operation_intent(
        self,
        identity: ApiIdentity,
        *,
        wallet: Mapping[str, Any],
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> PositionOperationIntent:
        try:
            operation_type = PositionOperationType(
                str(payload.get("operation_type") or "").upper()
            )
            amount = Decimal(str(payload.get("amount")))
        except (ValueError, TypeError) as exc:
            raise PaperApiError(
                "PAPER_POSITION_OPERATION_VALIDATION_ERROR",
                "operation_type or amount is invalid",
                status_code=400,
            ) from exc
        if not amount.is_finite() or amount <= 0:
            raise PaperApiError(
                "PAPER_POSITION_OPERATION_VALIDATION_ERROR",
                "operation amount must be positive and finite",
                status_code=400,
            )
        if operation_type is PositionOperationType.REDEEM:
            raise PaperApiError(
                "PAPER_REDEEM_LIFECYCLE_MANAGED",
                "Paper redeem is applied only after confirmed resolution finality",
                status_code=409,
                details={"endpoint": "/v1/paper/retail/lifecycle"},
            )
        decision_ts = datetime.now(timezone.utc)
        if operation_type in {
            PositionOperationType.SPLIT,
            PositionOperationType.MERGE,
        }:
            market_slug = str(payload.get("market_slug") or "").strip()
            if not market_slug:
                raise PaperApiError(
                    "PAPER_POSITION_OPERATION_VALIDATION_ERROR",
                    "market_slug is required for split or merge",
                    status_code=400,
                )
            market = self.retail_service.get_market(
                market_slug,
                principal=identity.principal,
            )
            outcomes = list(market.get("outcomes") or [])
            asset_ids = sorted(
                {str(row.get("asset_id") or "").strip() for row in outcomes}
                - {""}
            )
            if len(asset_ids) != 2 or not market.get("condition_id"):
                raise PaperApiError(
                    "PAPER_POSITION_OPERATION_UNSUPPORTED_MARKET",
                    "split and merge require one binary condition with two assets",
                    status_code=409,
                )
            phase = str(dict(market.get("lifecycle") or {}).get("phase") or "").upper()
            if phase in {
                "RESOLUTION_FINAL",
                "FINALIZED",
                "REDEEMABLE",
                "REDEEMING",
                "REDEEMED",
            }:
                raise PaperApiError(
                    "PAPER_POSITION_OPERATION_MARKET_FINAL",
                    "split or merge is unavailable after final resolution",
                    status_code=409,
                )
            direction = Decimal(1) if operation_type is PositionOperationType.SPLIT else Decimal(-1)
            return PositionOperationIntent(
                event_id=operation_id,
                operation_type=operation_type,
                account_id=str(wallet["account_id"]),
                strategy_id=str(wallet["ledger_strategy_id"]),
                condition_id=str(market["condition_id"]),
                market_id=str(market.get("market_id") or market_slug),
                amount=amount,
                decision_ts=decision_ts,
                collateral_delta=(-amount if direction > 0 else amount),
                token_deltas={asset_id: direction * amount for asset_id in asset_ids},
            )
        matrix_id = str(payload.get("matrix_id") or "").strip()
        if not matrix_id:
            raise PaperApiError(
                "PAPER_POSITION_OPERATION_VALIDATION_ERROR",
                "matrix_id is required for negative-risk conversion",
                status_code=400,
            )
        with self.tenant_store._transaction(
            identity.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT matrix.matrix_id,matrix.event_version_id,
                       matrix.token_deltas_per_unit,matrix.rule_version,
                       source.condition_id,event.event_key,event.version
                FROM quant.simulator_augmented_conversion_matrices matrix
                JOIN quant.simulator_augmented_neg_risk_events event
                  ON event.event_version_id=matrix.event_version_id
                JOIN quant.simulator_augmented_neg_risk_outcomes source
                  ON source.event_version_id=matrix.event_version_id
                 AND source.outcome_id=matrix.source_outcome_id
                WHERE matrix.matrix_id=%s AND source.active AND source.tradeable
                  AND event.version=(
                      SELECT max(latest.version)
                      FROM quant.simulator_augmented_neg_risk_events latest
                      WHERE latest.event_key=event.event_key
                  )
                """,
                (matrix_id,),
            )
            matrix = cur.fetchone()
            conn.commit()
        if matrix is None:
            raise PaperApiError(
                "PAPER_POSITION_OPERATION_MATRIX_NOT_FOUND",
                "active negative-risk conversion matrix was not found",
                status_code=404,
            )
        deltas = {
            str(asset_id): Decimal(str(per_unit)) * amount
            for asset_id, per_unit in dict(matrix["token_deltas_per_unit"]).items()
        }
        return PositionOperationIntent(
            event_id=operation_id,
            operation_type=PositionOperationType.NEG_RISK_CONVERT,
            account_id=str(wallet["account_id"]),
            strategy_id=str(wallet["ledger_strategy_id"]),
            condition_id=str(matrix["condition_id"]),
            market_id=str(matrix["event_version_id"]),
            amount=amount,
            decision_ts=decision_ts,
            collateral_delta=Decimal(0),
            token_deltas=deltas,
        )

    def _require_position_operation_admission(
        self,
        identity: ApiIdentity,
        intent: PositionOperationIntent,
    ) -> None:
        service = self.unified_admission_service
        if self.unified_admission_enforce and service is None:
            raise PaperApiError(
                "PAPER_ELIGIBILITY_UNAVAILABLE",
                "unified admission is enforced but unavailable",
                status_code=503,
            )
        if service is None or not (
            self.unified_admission_shadow or self.unified_admission_enforce
        ):
            return
        exposure_effect = {
            PositionOperationType.SPLIT: ExposureEffect.MIXED,
            PositionOperationType.MERGE: ExposureEffect.REDUCE,
            PositionOperationType.REDEEM: ExposureEffect.REDUCE,
            PositionOperationType.NEG_RISK_CONVERT: ExposureEffect.MIXED,
        }[intent.operation_type]
        decision = service.decide(
            AdmissionRequest(
                request_id=f"paper-position-operation-admission:{intent.event_id}",
                operation=AdmissionOperation(intent.operation_type.value),
                account_id=intent.account_id,
                strategy_id=intent.strategy_id,
                condition_id=intent.condition_id,
                market_id=intent.market_id,
                exposure_effect=exposure_effect,
                observed_at=intent.decision_ts,
                metadata={
                    "source": "paper_retail_api",
                    "operation_id": intent.event_id,
                    "amount": str(intent.amount),
                },
            )
        )
        if self.unified_admission_enforce and not decision.allowed:
            raise PaperApiError(
                "PAPER_ELIGIBILITY_REJECTED",
                "paper position operation was rejected by unified admission: "
                + ",".join(decision.reason_codes),
                status_code=403,
            )

    def _advance_retail_position_operation(self, operation_id: str) -> Any:
        """Resume the durable Paper-only lifecycle after any process interruption."""

        nonce = int(hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:15], 16)
        for _ in range(8):
            record = self.position_operation_store.operation(operation_id)
            if record is None:
                raise LookupError("paper position operation disappeared")
            now = datetime.now(timezone.utc)
            if record.state is PositionOperationState.CREATED:
                self.position_operation_store.allowance_checked(
                    operation_id,
                    event_id=f"{operation_id}:paper-resources-checked",
                    event_ts=now,
                    approved=True,
                    reason="paper_resources_checked",
                )
            elif record.state is PositionOperationState.ALLOWANCE_CHECKED:
                self.position_operation_store.reserve_nonce(
                    operation_id,
                    event_id=f"{operation_id}:paper-sequence-reserved",
                    event_ts=now,
                    nonce=nonce,
                )
            elif record.state is PositionOperationState.NONCE_RESERVED:
                self.position_operation_store.submit(
                    operation_id,
                    event_id=f"{operation_id}:paper-submitted",
                    event_ts=now,
                    transaction_hash=f"paper:{operation_id}",
                )
            elif record.state is PositionOperationState.SUBMITTED:
                self.position_operation_store.mined(
                    operation_id,
                    event_id=f"{operation_id}:paper-applied",
                    event_ts=now,
                )
            elif record.state is PositionOperationState.MINED:
                try:
                    self.position_operation_store.confirm(
                        operation_id,
                        event_id=f"{operation_id}:paper-confirmed",
                        event_ts=now,
                    )
                except PositionOperationReservationError as exc:
                    raise PaperApiError(
                        "PAPER_POSITION_OPERATION_RESOURCES_UNAVAILABLE",
                        str(exc),
                        status_code=409,
                    ) from exc
            elif record.state is PositionOperationState.CONFIRMED:
                self.position_operation_store.reconcile(
                    operation_id,
                    event_id=f"{operation_id}:paper-reconciled",
                    event_ts=now,
                    reason="paper_receipt_reconciled",
                )
            elif record.state is PositionOperationState.RECONCILED:
                return record
            elif record.state is PositionOperationState.FAILED:
                raise PaperApiError(
                    "PAPER_POSITION_OPERATION_FAILED",
                    str(record.reason or "paper position operation failed"),
                    status_code=409,
                )
        raise PaperApiError(
            "PAPER_POSITION_OPERATION_STALLED",
            "paper position operation did not reach a terminal state",
            status_code=503,
        )

    @staticmethod
    def _serialize_position_operation_row(row: dict[str, Any]) -> dict[str, Any]:
        transaction_ref = str(row.pop("transaction_hash", "") or "")
        row["paper_receipt_ref"] = transaction_ref or None
        row["chain_transaction_hash"] = None
        row["is_chain_transaction"] = False
        return row

    @classmethod
    def _serialize_position_operation_record(
        cls,
        record: Any,
        *,
        events: Sequence[Mapping[str, Any]],
        reservation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "operation_id": record.intent.event_id,
            "operation_type": record.intent.operation_type.value,
            "account_id": record.intent.account_id,
            "strategy_id": record.intent.strategy_id,
            "market_id": record.intent.market_id,
            "condition_id": record.intent.condition_id,
            "amount": record.intent.amount,
            "collateral_delta": record.intent.collateral_delta,
            "token_deltas": dict(record.intent.token_deltas),
            "decision_ts": record.intent.decision_ts,
            "state": record.state.value,
            "paper_receipt_ref": record.transaction_hash,
            "chain_transaction_hash": None,
            "is_chain_transaction": False,
            "reason": record.reason,
            "reservation": dict(reservation or {}),
            "events": [dict(event) for event in events],
        }

    def admin_dashboard(self, identity: ApiIdentity) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.dashboard(identity.principal)

    def list_admin_jobs(
        self, identity: ApiIdentity, *, limit: int
    ) -> list[dict[str, Any]]:
        identity.require_scope("paper:admin")
        return self.admin_service.list_jobs(identity.principal, limit=limit)

    def set_tenant_freeze(
        self, identity: ApiIdentity, *, frozen: bool, reason: str
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.set_tenant_freeze(
            identity.principal, frozen=frozen, reason=reason
        )

    def set_account_freeze(
        self,
        identity: ApiIdentity,
        account_id: UUID,
        *,
        frozen: bool,
        reason: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.set_account_freeze(
            identity.principal, account_id, frozen=frozen, reason=reason
        )

    def kill_account(
        self, identity: ApiIdentity, account_id: UUID, *, reason: str
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.kill_account(
            identity.principal, account_id, reason=reason
        )

    def reconcile_account(
        self,
        identity: ApiIdentity,
        account_id: UUID,
        *,
        mode: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.reconcile_account(
            identity.principal,
            account_id,
            mode=mode,
            idempotency_key=f"api:{idempotency_key}",
        )

    def list_dlq(self, identity: ApiIdentity, *, limit: int) -> list[dict[str, Any]]:
        identity.require_scope("paper:admin")
        return self.admin_service.list_dlq(identity.principal, limit=limit)

    def replay_dlq_event(
        self,
        identity: ApiIdentity,
        dlq_event_id: UUID,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.replay_dlq_event(
            identity.principal,
            dlq_event_id,
            idempotency_key=f"api:{idempotency_key}",
        )

    def ignore_dlq_event(
        self, identity: ApiIdentity, dlq_event_id: UUID, *, reason: str
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.ignore_dlq_event(
            identity.principal, dlq_event_id, reason=reason
        )

    def list_notices(self, identity: ApiIdentity) -> list[dict[str, Any]]:
        identity.require_scope("paper:admin")
        return self.admin_service.list_notices(identity.principal)

    def create_notice(
        self,
        identity: ApiIdentity,
        *,
        title: str,
        message: str,
        starts_at: datetime,
        ends_at: datetime | None,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.create_notice(
            identity.principal,
            title=title,
            message=message,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    def set_notice_status(
        self, identity: ApiIdentity, notice_id: UUID, *, status: str
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.set_notice_status(
            identity.principal, notice_id, status=status
        )

    def list_incidents(self, identity: ApiIdentity) -> list[dict[str, Any]]:
        identity.require_scope("paper:admin")
        return self.admin_service.list_incidents(identity.principal)

    def create_incident(
        self,
        identity: ApiIdentity,
        *,
        title: str,
        summary: str,
        severity: str,
        started_at: datetime,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.create_incident(
            identity.principal,
            title=title,
            summary=summary,
            severity=severity,
            started_at=started_at,
        )

    def add_incident_note(
        self, identity: ApiIdentity, incident_id: UUID, *, body: str
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.add_incident_note(
            identity.principal, incident_id, body=body
        )

    def set_incident_status(
        self, identity: ApiIdentity, incident_id: UUID, *, status: str
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.set_incident_status(
            identity.principal, incident_id, status=status
        )

    def list_retention_policies(self, identity: ApiIdentity) -> list[dict[str, Any]]:
        identity.require_scope("paper:admin")
        return self.admin_service.list_retention_policies(identity.principal)

    def upsert_retention_policy(
        self,
        identity: ApiIdentity,
        *,
        resource_type: str,
        retention_days: int,
        legal_hold: bool,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.upsert_retention_policy(
            identity.principal,
            resource_type=resource_type,
            retention_days=retention_days,
            legal_hold=legal_hold,
        )

    def run_retention(
        self,
        identity: ApiIdentity,
        *,
        resource_type: str,
        mode: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.run_retention(
            identity.principal,
            resource_type=resource_type,
            mode=mode,
            idempotency_key=f"api:{idempotency_key}",
        )

    def list_evidence_bundles(
        self, identity: ApiIdentity, *, limit: int
    ) -> list[dict[str, Any]]:
        identity.require_scope("paper:admin")
        return self.admin_service.list_evidence_bundles(identity.principal, limit=limit)

    def create_evidence_bundle(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID | None,
        incident_id: UUID | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity.require_scope("paper:admin")
        return self.admin_service.create_evidence_bundle(
            identity.principal,
            account_id=account_id,
            incident_id=incident_id,
            idempotency_key=f"api:{idempotency_key}",
        )

    def get_evidence_bundle(
        self, identity: ApiIdentity, bundle_id: UUID
    ) -> tuple[dict[str, Any], bytes]:
        identity.require_scope("paper:admin")
        return self.admin_service.get_evidence_bundle(identity.principal, bundle_id)

    def record_request(
        self,
        identity: ApiIdentity,
        *,
        request_id: UUID,
        method: str,
        route: str,
        response_status: int,
        latency_ms: Decimal,
        error_code: str | None,
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_tenant_id', %s, true)",
                (str(identity.principal.tenant_id),),
            )
            cur.execute(
                """
                INSERT INTO quant.paper_api_request_log (
                    request_id,tenant_id,user_id,api_key_id,method,route,
                    response_status,latency_ms,error_code
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    request_id,
                    identity.principal.tenant_id,
                    identity.principal.subject_user_id,
                    identity.api_key_id,
                    str(method),
                    str(route),
                    int(response_status),
                    max(Decimal(0), latency_ms),
                    error_code,
                ),
            )
            conn.commit()


def translate_domain_error(error: Exception) -> PaperApiError:
    if isinstance(error, PaperApiError):
        return error
    if isinstance(error, ReplayValidationError):
        return PaperApiError(
            "PAPER_REPLAY_VALIDATION_ERROR", str(error), status_code=400
        )
    if isinstance(error, ReplayNotFound):
        return PaperApiError("PAPER_REPLAY_NOT_FOUND", str(error), status_code=404)
    if isinstance(error, ReplayStateConflict):
        return PaperApiError("PAPER_REPLAY_CONFLICT", str(error), status_code=409)
    if isinstance(error, ReplayCapacityExceeded):
        return PaperApiError(
            "PAPER_REPLAY_CAPACITY_EXCEEDED", str(error), status_code=429
        )
    if isinstance(error, ReplayServiceError):
        return PaperApiError("PAPER_REPLAY_ERROR", str(error), status_code=409)
    if isinstance(error, ScenarioValidationError):
        return PaperApiError(
            "PAPER_SCENARIO_VALIDATION_ERROR", str(error), status_code=400
        )
    if isinstance(error, ScenarioNotFound):
        return PaperApiError("PAPER_SCENARIO_NOT_FOUND", str(error), status_code=404)
    if isinstance(error, ScenarioServiceError):
        return PaperApiError("PAPER_SCENARIO_ERROR", str(error), status_code=409)
    if isinstance(error, ConditionalOrderValidationError):
        return PaperApiError(
            "PAPER_CONDITIONAL_VALIDATION_ERROR", str(error), status_code=400
        )
    if isinstance(error, ConditionalOrderNotFound):
        return PaperApiError("PAPER_CONDITIONAL_NOT_FOUND", str(error), status_code=404)
    if isinstance(error, ConditionalOrderConflict):
        return PaperApiError("PAPER_CONDITIONAL_CONFLICT", str(error), status_code=409)
    if isinstance(error, ConditionalOrderError):
        return PaperApiError("PAPER_CONDITIONAL_ERROR", str(error), status_code=409)
    if isinstance(error, PaperAdminValidationError):
        return PaperApiError(
            "PAPER_ADMIN_VALIDATION_ERROR", str(error), status_code=400
        )
    if isinstance(error, PaperAdminNotFound):
        return PaperApiError("PAPER_ADMIN_NOT_FOUND", str(error), status_code=404)
    if isinstance(error, PaperAdminConflict):
        return PaperApiError("PAPER_ADMIN_CONFLICT", str(error), status_code=409)
    if isinstance(error, PaperAdminError):
        return PaperApiError("PAPER_ADMIN_ERROR", str(error), status_code=503)
    if isinstance(error, PaperQuotaExceeded):
        return PaperApiError(
            "PAPER_RATE_LIMITED",
            str(error),
            status_code=429,
        )
    if isinstance(error, PaperAuthorizationError):
        return PaperApiError("PAPER_FORBIDDEN", str(error), status_code=403)
    if isinstance(error, TenantScopeError):
        return PaperApiError("PAPER_NOT_FOUND", str(error), status_code=404)
    if isinstance(error, TenantPlatformError):
        return PaperApiError("PAPER_CONFLICT", str(error), status_code=409)
    if isinstance(error, LookupError):
        return PaperApiError("PAPER_NOT_FOUND", str(error), status_code=404)
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return PaperApiError(
            "PAPER_VALIDATION_ERROR",
            str(error),
            status_code=400,
        )
    return PaperApiError(
        "PAPER_INTERNAL",
        "internal paper API error",
        status_code=500,
    )
