# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app + routes for the identity service.

Mirrors ``CONTRACTS.md → Identity Service`` 1:1.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, Query, Request, Response, status

from . import __service__, __version__
from .exceptions import (
    InvalidArguments,
    InvalidToken,
    SigningKeyNotFound,
    TokenExpired,
    TokenRevoked,
    install_exception_handlers,
)
from .jwt_io import HS256, RS256, TokenManager
from .keys import KeyStore
from .logging_config import configure_logging, get_logger
from .migration_runner import (
    MigrationLockError,
    MigrationRollbackMissing as RunnerRollbackMissing,
    MigrationRunner,
    default_migrations_dir,
)
from .models import (
    HealthResponse,
    JWKSResponse,
    RevocationList,
    RevocationStats,
    RollbackBody,
    RollbackResult,
    RolledBackMigrationModel,
    SigningKey,
    SigningKeyList,
    Tenant,
    TenantCreate,
    TenantList,
    TokenClaims,
    TokenInfo,
    TokenInfoList,
    TokenIssueRequest,
    TokenIssueResponse,
    TokenVerifyRequest,
)
from .settings import Settings, get_settings
from .store import TenantStore, TokenStore, init_db

UTC = timezone.utc  # noqa: UP017

# How often the background task checks whether the active key is past its
# rotation window. One hour matches the spec — short enough to spread the
# rotation work, long enough that most ticks are no-ops.
ROTATION_CHECK_SECONDS = 3600


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Tests build a fresh app per session with a tmp-dir backed ``Settings``;
    production goes through ``__main__`` which uses the env-driven default.
    """

    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await init_db(settings.db_path)

        # v0.5 — schema migrations. Apply pending migrations after init_db
        # (idempotent CREATE-IF-NOT-EXISTS bootstrap) so existing v0.3–v0.4
        # databases get marked-as-applied without re-running SQL. The
        # ``database_url`` parameter selects the v0.6 Postgres advisory-lock
        # path; empty keeps the SQLite ``fcntl.flock`` path.
        runner = MigrationRunner(
            settings.db_path,
            default_migrations_dir(__file__),
            database_url=settings.effective_database_url,
            service_name="identity",
        )
        app.state.migration_runner = runner
        try:
            if settings.auto_migrate:
                applied_migs = await runner.apply_pending(blocking_lock=True)
                if applied_migs:
                    log.info(
                        "identity.migrations.applied",
                        count=len(applied_migs),
                        ids=[a.id for a in applied_migs],
                    )
            else:
                pending_migs = await runner.list_pending()
                if pending_migs:
                    log.warning(
                        "identity.migrations.pending",
                        count=len(pending_migs),
                        ids=[m.id for m in pending_migs],
                        hint=(
                            "auto_migrate is disabled. Run "
                            "`python -m plinth_identity migrate` to apply."
                        ),
                    )
        except MigrationLockError as exc:
            log.warning("identity.migrations.locked", error=str(exc))

        alg = settings.identity_jwt_alg
        manager: TokenManager
        key_store: KeyStore | None = None

        if alg == HS256:
            secret_provided = bool(settings.identity_jwt_secret)
            secret_existed_on_disk = settings.secret_path.exists()
            secret = settings.resolve_secret()
            if not secret_provided:
                log.warning(
                    "identity.jwt_secret.auto_managed",
                    source="disk" if secret_existed_on_disk else "generated",
                    path=str(settings.secret_path),
                    hint=(
                        "Set PLINTH_IDENTITY_JWT_SECRET to a 32-byte base64 "
                        "string in production deployments."
                    ),
                )
            manager = TokenManager(
                secret=secret,
                issuer=settings.identity_url,
                audience=settings.identity_jwt_audience,
                alg=HS256,
            )
        else:
            # RS256 — provision the keys schema, ensure an active key
            # exists, surface the encryption-key source the same way we do
            # for HS256 so operators can audit dev mode.
            enc_provided = bool(settings.identity_keys_encryption_key)
            enc_existed_on_disk = settings.keys_encryption_key_path.exists()
            # Resolve eagerly so a misconfigured env var fails fast.
            settings.resolve_keys_encryption_key()
            if not enc_provided:
                log.warning(
                    "identity.keys_encryption_key.auto_managed",
                    source="disk" if enc_existed_on_disk else "generated",
                    path=str(settings.keys_encryption_key_path),
                    hint=(
                        "Set PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY to a base64 "
                        "32-byte AES key in production deployments."
                    ),
                )
            key_store = KeyStore(settings.db_path, settings)
            await key_store.init()
            manager = TokenManager(
                issuer=settings.identity_url,
                audience=settings.identity_jwt_audience,
                alg=RS256,
                key_store=key_store,
            )

        app.state.token_manager = manager
        app.state.key_store = key_store
        app.state.store = TokenStore(settings.db_path)
        app.state.tenants = TenantStore(settings.db_path)
        # Warm the revocation cache so the first verify doesn't pay disk cost.
        await app.state.store.reload_cache()
        log.info(
            "identity.startup",
            data_dir=str(settings.data_dir),
            db_path=str(settings.db_path),
            issuer=settings.identity_url,
            audience=settings.identity_jwt_audience,
            port=settings.identity_port,
            alg=alg,
        )

        rotation_task: asyncio.Task[None] | None = None
        if alg == RS256 and key_store is not None:
            rotation_task = asyncio.create_task(
                _rotation_loop(key_store, ROTATION_CHECK_SECONDS, log),
                name="identity.key_rotation_loop",
            )
            app.state.rotation_task = rotation_task

        try:
            yield
        finally:
            if rotation_task is not None:
                rotation_task.cancel()
                try:
                    await rotation_task
                except (asyncio.CancelledError, Exception):
                    # Cancellation is expected; any other exception we log.
                    pass
            log.info("identity.shutdown")

    app = FastAPI(
        title="plinth-identity",
        version=__version__,
        description="Plinth identity service — JWT capability tokens.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    # v0.5 — migration runner. Constructed eagerly so admin endpoints
    # work even in test setups that bypass the lifespan handler. Forwards
    # ``database_url`` + ``service_name`` for the v0.6 Postgres advisory-lock
    # path; no-op for the SQLite default deployments.
    app.state.migration_runner = MigrationRunner(
        settings.db_path,
        default_migrations_dir(__file__),
        database_url=settings.effective_database_url,
        service_name="identity",
    )
    install_exception_handlers(app)
    app.middleware("http")(_request_context_middleware)

    _register_routes(app)
    return app


async def _rotation_loop(
    key_store: KeyStore,
    interval_seconds: int,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Periodically call :meth:`KeyStore.auto_rotate_if_due`.

    Handles cancellation cleanly. Errors are logged and swallowed so a
    transient SQLite glitch doesn't crash the loop forever.
    """

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            rotated = await key_store.auto_rotate_if_due()
            if rotated is not None:
                log.info(
                    "identity.keys.rotated",
                    kid=rotated.kid,
                    expires_at=rotated.expires_at.isoformat(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("identity.keys.rotation_error", error=str(exc))


# ---------------------------------------------------------------------------
# Middleware


async def _request_context_middleware(request: Request, call_next):
    """Attach a request_id + service context to every log line."""

    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        service=__service__,
        request_id=request_id,
        path=request.url.path,
        method=request.method,
    )
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# ---------------------------------------------------------------------------
# Dependencies


def _get_manager(request: Request) -> TokenManager:
    return request.app.state.token_manager


def _get_store(request: Request) -> TokenStore:
    return request.app.state.store


def _get_tenants(request: Request) -> TenantStore:
    return request.app.state.tenants


def _get_key_store(request: Request) -> KeyStore | None:
    return request.app.state.key_store


ManagerDep = Annotated[TokenManager, Depends(_get_manager)]
StoreDep = Annotated[TokenStore, Depends(_get_store)]
TenantsDep = Annotated[TenantStore, Depends(_get_tenants)]


def _signing_key_response(key) -> SigningKey:
    """Project the keys-module model onto the API model (same shape)."""

    # Pydantic models — round-trip via dict to detach from any subclassing.
    return SigningKey(
        kid=key.kid,
        alg=key.alg,
        public_key_pem=key.public_key_pem,
        created_at=key.created_at,
        rotated_in_at=key.rotated_in_at,
        expires_at=key.expires_at,
        active=key.active,
    )


# ---------------------------------------------------------------------------
# Routes


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz", response_model=HealthResponse, tags=["meta"])
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, service=__service__)

    @app.get(
        "/v1/.well-known/jwks.json",
        response_model=JWKSResponse,
        tags=["meta"],
    )
    async def jwks(request: Request) -> JWKSResponse:
        # HS256 → empty keys list (the secret is shared, not published).
        # RS256 → the most recent ``jwks_max_keys`` non-expired public keys.
        key_store: KeyStore | None = request.app.state.key_store
        if key_store is None:
            return JWKSResponse(keys=[])
        keys = await key_store.list_jwks_keys()
        return JWKSResponse(keys=[key_store.to_jwk(k) for k in keys])

    # ------------------------------------------------------------------ tokens

    @app.post(
        "/v1/tokens",
        response_model=TokenIssueResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["tokens"],
    )
    async def issue_token(
        body: TokenIssueRequest,
        manager: ManagerDep,
        store: StoreDep,
        request: Request,
    ) -> TokenIssueResponse:
        if not body.agent_id:
            raise InvalidArguments(
                "agent_id is required",
                details={"field": "agent_id"},
            )
        if body.ttl_seconds < 1:
            raise InvalidArguments(
                "ttl_seconds must be >= 1",
                details={"ttl_seconds": body.ttl_seconds},
            )
        max_ttl = request.app.state.settings.identity_jwt_max_ttl_seconds
        if body.ttl_seconds > max_ttl:
            raise InvalidArguments(
                f"ttl_seconds {body.ttl_seconds} exceeds max {max_ttl}",
                details={"ttl_seconds": body.ttl_seconds, "max": max_ttl},
            )

        # ``issue_async`` covers both HS256 and RS256 so this code path
        # stays algorithm-agnostic.
        issued = await manager.issue_async(
            agent_id=body.agent_id,
            tenant_id=body.tenant_id,
            scopes=body.scopes,
            workspace_id=body.workspace_id,
            ttl_seconds=body.ttl_seconds,
            rate_limit=body.rate_limit,
        )
        claims = issued.claims

        # Persist metadata for introspection + revocation.
        info = await store.insert(
            jti=claims.jti,
            agent_id=claims.agent_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            scopes=claims.scopes,
            issued_at=datetime.fromtimestamp(claims.iat, tz=UTC),
            expires_at=datetime.fromtimestamp(claims.exp, tz=UTC),
            metadata=body.metadata,
        )
        get_logger().info(
            "identity.token.issued",
            jti=claims.jti,
            agent_id=claims.agent_id,
            tenant_id=claims.tenant_id,
            scopes=list(claims.scopes),
            alg=manager.alg,
        )
        return TokenIssueResponse(
            token=issued.token,
            jti=claims.jti,
            expires_at=info.expires_at,
            claims=claims,
        )

    @app.post(
        "/v1/tokens/verify",
        response_model=TokenClaims,
        tags=["tokens"],
    )
    async def verify_token(
        body: TokenVerifyRequest,
        manager: ManagerDep,
        store: StoreDep,
    ) -> TokenClaims:
        if not body.token:
            raise InvalidArguments(
                "token is required",
                details={"field": "token"},
            )

        try:
            claims = await manager.decode_async(body.token)
        except TokenExpired:
            raise
        except InvalidToken:
            raise

        if await store.is_revoked(claims.jti):
            raise TokenRevoked(
                f"Token {claims.jti} has been revoked",
                details={"jti": claims.jti},
            )

        return claims

    @app.post(
        "/v1/tokens/{jti}/revoke",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["tokens"],
    )
    async def revoke_token(
        jti: str,
        store: StoreDep,
    ) -> Response:
        await store.revoke(jti)
        get_logger().info("identity.token.revoked", jti=jti)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/tokens",
        response_model=TokenInfoList,
        tags=["tokens"],
    )
    async def list_tokens(
        store: StoreDep,
        revoked: Annotated[bool | None, Query()] = None,
        since: Annotated[str | None, Query()] = None,
        agent_id: Annotated[str | None, Query()] = None,
        tenant_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
    ) -> TokenInfoList:
        """List tokens, primarily for revocation polling.

        Workspace and Gateway poll ``?revoked=true&since=<ts>`` every
        ``revocation_poll_seconds`` to refresh their in-memory blocklist.
        """

        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidArguments(
                    f"invalid 'since' timestamp: {since!r}",
                    details={"since": since},
                ) from exc
        tokens = await store.list_tokens(
            revoked=revoked,
            since=since_dt,
            agent_id=agent_id,
            tenant_id=tenant_id,
            limit=limit,
        )
        return TokenInfoList(tokens=tokens)

    @app.get(
        "/v1/tokens/{jti}",
        response_model=TokenInfo,
        tags=["tokens"],
    )
    async def get_token_info(
        jti: str,
        store: StoreDep,
    ) -> TokenInfo:
        return await store.get(jti)

    # ---------------------------------------------- v0.6 federated revocation
    #
    # Replica-friendly endpoints for cross-node revocation propagation.
    # Other services (workspace, gateway) poll ``/v1/revocations`` every
    # ~60s with a ``since=<unix_ts>`` cursor and replay any new entries
    # into their in-memory blocklist. Read-only; no admin scope required.

    @app.get(
        "/v1/revocations",
        response_model=RevocationList,
        tags=["tokens"],
    )
    async def list_revocations(
        store: StoreDep,
        since: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
    ) -> RevocationList:
        """List revoked tokens with ``revoked_at > since`` (unix seconds).

        Acts as a forward cursor: the response's ``next_since`` is the
        unix-second timestamp of the last entry returned, suitable to use
        as the next call's ``since``. ``has_more`` signals there are
        further pages immediately available.
        """

        entries, has_more = await store.list_revocations(
            since_unix=since,
            limit=limit,
        )
        if entries:
            last = entries[-1].revoked_at
            next_since = int(last.timestamp())
        else:
            next_since = since
        return RevocationList(
            revocations=entries,
            next_since=next_since,
            has_more=has_more,
        )

    @app.get(
        "/v1/revocations/stats",
        response_model=RevocationStats,
        tags=["tokens"],
    )
    async def revocation_stats(store: StoreDep) -> RevocationStats:
        """Return cheap counters about revoked tokens."""

        total, since_24h, since_1h = await store.revocation_stats()
        return RevocationStats(
            total=total,
            since_24h=since_24h,
            since_1h=since_1h,
        )

    # ------------------------------------------------------------------ tenants

    @app.get(
        "/v1/tenants",
        response_model=TenantList,
        tags=["tenants"],
    )
    async def list_tenants(tenants: TenantsDep) -> TenantList:
        return TenantList(tenants=await tenants.list())

    @app.post(
        "/v1/tenants",
        response_model=Tenant,
        status_code=status.HTTP_201_CREATED,
        tags=["tenants"],
    )
    async def create_tenant(
        body: TenantCreate,
        tenants: TenantsDep,
    ) -> Tenant:
        if not body.id.replace("-", "").replace("_", "").isalnum():
            raise InvalidArguments(
                "tenant id must be alphanumeric (with - and _ allowed)",
                details={"id": body.id},
            )
        tenant = await tenants.create(
            tenant_id=body.id,
            name=body.name,
            metadata=body.metadata,
        )
        get_logger().info(
            "identity.tenant.created",
            tenant_id=tenant.id,
            name=tenant.name,
        )
        return tenant

    @app.get(
        "/v1/tenants/{tenant_id}",
        response_model=Tenant,
        tags=["tenants"],
    )
    async def get_tenant(
        tenant_id: str,
        tenants: TenantsDep,
    ) -> Tenant:
        return await tenants.get(tenant_id)

    # -------------------------------------------------------------------- keys

    @app.get(
        "/v1/keys",
        response_model=SigningKeyList,
        tags=["keys"],
    )
    async def list_signing_keys(
        request: Request,
        include_expired: Annotated[bool, Query()] = False,
    ) -> SigningKeyList:
        """List RS256 signing keys (public material only).

        For an HS256-only deployment this returns an empty list — there
        are no published keys.
        """

        key_store: KeyStore | None = request.app.state.key_store
        if key_store is None:
            return SigningKeyList(keys=[])
        keys = await key_store.list_keys(include_expired=include_expired)
        return SigningKeyList(keys=[_signing_key_response(k) for k in keys])

    @app.post(
        "/v1/keys/rotate",
        response_model=SigningKey,
        status_code=status.HTTP_201_CREATED,
        tags=["keys"],
    )
    async def rotate_signing_key(request: Request) -> SigningKey:
        """Force a key rotation.

        In a production deployment a scope-checking middleware would gate
        this on ``tenant:*:admin`` or ``*``. We surface a hard 400 here so
        a misconfigured HS256 deployment doesn't silently accept the call.
        """

        key_store: KeyStore | None = request.app.state.key_store
        if key_store is None:
            raise InvalidArguments(
                "key rotation is only available when jwt_alg=RS256",
                details={"jwt_alg": request.app.state.settings.identity_jwt_alg},
            )
        new_key = await key_store.rotate()
        get_logger().info(
            "identity.keys.rotated",
            kid=new_key.kid,
            forced=True,
        )
        return _signing_key_response(new_key)

    @app.delete(
        "/v1/keys/{kid}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["keys"],
    )
    async def expire_signing_key(kid: str, request: Request) -> Response:
        """Force-expire a signing key by ``kid`` (incident response)."""

        key_store: KeyStore | None = request.app.state.key_store
        if key_store is None:
            raise InvalidArguments(
                "key expiry is only available when jwt_alg=RS256",
                details={"jwt_alg": request.app.state.settings.identity_jwt_alg},
            )
        try:
            await key_store.expire(kid)
        except KeyError as exc:
            raise SigningKeyNotFound(kid) from exc
        get_logger().info("identity.keys.expired", kid=kid)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ----------------------------------------------------------- migrations

    @app.get(
        "/v1/admin/migrations",
        tags=["meta"],
    )
    async def list_migrations(request: Request) -> dict:
        runner: MigrationRunner = request.app.state.migration_runner
        status_obj = await runner.status()
        return {
            "current": status_obj.current,
            "applied": [_serialize_applied(m) for m in status_obj.applied],
            "pending": [_serialize_pending(m) for m in status_obj.pending],
            "mismatches": [
                {
                    "id": mm.id,
                    "stored_checksum": mm.stored_checksum,
                    "current_checksum": mm.current_checksum,
                }
                for mm in status_obj.mismatches
            ],
        }

    @app.post(
        "/v1/admin/migrations/apply",
        status_code=status.HTTP_201_CREATED,
        tags=["meta"],
    )
    async def apply_migrations(request: Request):
        runner: MigrationRunner = request.app.state.migration_runner
        try:
            applied_migs = await runner.apply_pending(blocking_lock=False)
        except MigrationLockError as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "MIGRATION_LOCKED",
                        "message": str(exc),
                        "details": {},
                    }
                },
            )
        return {"applied": [_serialize_applied(m) for m in applied_migs]}

    @app.post(
        "/v1/admin/migrations/rollback",
        response_model=RollbackResult,
        tags=["meta"],
    )
    async def rollback_migrations(
        body: RollbackBody,
        request: Request,
    ):
        """Roll back applied migrations down to (and including) ``body.to``.

        Returns 200 with the :class:`RollbackResult` payload (even when one
        of the rollback files errored mid-way — ``failed`` and
        ``error_message`` carry the partial-state info).
        """

        runner: MigrationRunner = request.app.state.migration_runner
        try:
            outcome = await runner.rollback_to(
                body.to,
                dry_run=body.dry_run,
                blocking_lock=False,
            )
        except RunnerRollbackMissing as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "MIGRATION_ROLLBACK_MISSING",
                        "message": str(exc),
                        "details": {"missing_ids": exc.missing_ids},
                    }
                },
            )
        except MigrationLockError as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "MIGRATION_LOCKED",
                        "message": str(exc),
                        "details": {},
                    }
                },
            )

        return RollbackResult(
            target=outcome.target,
            rolled_back=[
                RolledBackMigrationModel(
                    id=entry.id,
                    rolled_back_at=entry.rolled_back_at,
                    duration_ms=entry.duration_ms,
                )
                for entry in outcome.rolled_back
            ],
            skipped=list(outcome.skipped),
            failed=outcome.failed,
            error_message=outcome.error_message,
            dry_run=outcome.dry_run,
        )


# Helpers for /v1/admin/migrations.


def _serialize_applied(mig) -> dict:  # noqa: ANN001
    return {
        "id": mig.id,
        "checksum": mig.checksum,
        "applied_at": mig.applied_at.isoformat(),
        "duration_ms": mig.duration_ms,
    }


def _serialize_pending(mig) -> dict:  # noqa: ANN001
    return {
        "id": mig.id,
        "checksum": mig.checksum,
    }


# A module-level default for environments that import
# ``plinth_identity.api:app``. Lazy: built only on first attribute access so
# tests that import ``create_app`` don't pay startup cost.

_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


__all__ = ["create_app"]
