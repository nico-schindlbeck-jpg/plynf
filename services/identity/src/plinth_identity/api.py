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
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, JSONResponse

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
from .metrics import (
    MetricsRegistry,
    metrics_middleware_factory,
    metrics_response,
)
from .migration_runner import (
    MigrationLockError,
    MigrationRollbackMissing as RunnerRollbackMissing,
    MigrationRunner,
    default_migrations_dir,
)
from .models import (
    DeleteConfirmation,
    DeleteJob,
    ExportJobAcknowledgement,
    ExportStatus,
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
from .quotas import (
    QuotaStore,
    TenantQuotas,
    TenantQuotasUpdate,
    TenantUsage,
)
from .regions import RegionsResponse, RegionStatusProbe
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
        app.state.quotas = QuotaStore(settings.db_path)
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
    # v1.0 — multi-region scaffolding. Identity's cross-region propagation
    # lever (revocation polling) is in v0.6; the probe here just powers
    # the ``/v1/regions`` discovery endpoint.
    app.state.region_status_probe = RegionStatusProbe(
        cache_ttl_seconds=settings.regions_status_cache_ttl_seconds,
        probe_timeout_seconds=settings.regions_status_probe_timeout_seconds,
    )
    # v1.0 — Prometheus metrics. Pre-declares identity-specific series
    # so scrapes against a fresh deployment return the canonical schema.
    metrics = MetricsRegistry(service_name=__service__, version=__version__)
    metrics.declare_counter(
        "plinth_tokens_issued_total",
        "Tokens issued (per tenant).",
    )
    metrics.declare_counter(
        "plinth_tokens_revoked_total",
        "Tokens revoked.",
    )
    metrics.declare_gauge(
        "plinth_tokens_active",
        "Active (non-revoked, non-expired) tokens.",
    )
    metrics.declare_counter(
        "plinth_token_verifications_total",
        "Token verifications by result (ok|expired|revoked|invalid).",
    )
    app.state.metrics = metrics
    install_exception_handlers(app)
    app.middleware("http")(metrics_middleware_factory(metrics))
    app.middleware("http")(_request_context_middleware)
    app.middleware("http")(_replica_redirect_middleware)

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


# v1.0 — read-replica redirect. Identity is mostly issue/verify/revoke;
# replicas legitimately serve verify (read) but must redirect issue +
# revoke to the primary. Token verification is intentionally idempotent
# so this works.
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_REPLICA_ALLOWLIST = (
    "/healthz",
    "/v1/regions",
    "/v1/.well-known/jwks.json",
    "/v1/tokens/verify",  # POST but read-only verification
    "/metrics",
)


async def _replica_redirect_middleware(request: Request, call_next):
    """Short-circuit mutating writes when ``replication_mode == 'replica'``.

    Emits 421 (Misdirected Request) with ``X-Plinth-Primary-Region`` +
    ``X-Plinth-Primary-URL`` so SDK clients can transparently retry. A
    standard ``Location`` header is added for plain-HTTP / curl users.
    """

    settings: Settings = request.app.state.settings
    if settings.replication_mode != "replica":
        return await call_next(request)
    if request.method not in _MUTATING_METHODS:
        return await call_next(request)
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _REPLICA_ALLOWLIST):
        return await call_next(request)

    primary_id = settings.region_peers[0] if settings.region_peers else settings.region_id
    primary_url = settings.region_primary_url or settings.region_peer_urls.get(primary_id, "")
    headers: dict[str, str] = {"X-Plinth-Primary-Region": primary_id}
    if primary_url:
        normalized = primary_url.rstrip("/")
        headers["X-Plinth-Primary-URL"] = normalized
        headers["Location"] = normalized + path
    return JSONResponse(
        status_code=421,
        content={
            "error": {
                "code": "REPLICA_READ_ONLY",
                "message": (
                    "this is a read-replica; submit mutating requests "
                    f"to {primary_url or primary_id}"
                ),
                "details": {
                    "region": settings.region_id,
                    "primary_region": primary_id,
                    "primary_url": primary_url or None,
                },
            }
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Dependencies


def _get_manager(request: Request) -> TokenManager:
    return request.app.state.token_manager


def _get_store(request: Request) -> TokenStore:
    return request.app.state.store


def _get_tenants(request: Request) -> TenantStore:
    return request.app.state.tenants


def _get_quotas(request: Request) -> QuotaStore:
    # ``app.state.quotas`` is set inside ``lifespan`` for production.
    # Tests that bypass the lifespan handler still work because the
    # store is a thin wrapper over ``settings.db_path``.
    quotas = getattr(request.app.state, "quotas", None)
    if quotas is None:
        quotas = QuotaStore(request.app.state.settings.db_path)
        request.app.state.quotas = quotas
    return quotas


def _get_key_store(request: Request) -> KeyStore | None:
    return request.app.state.key_store


def _get_compliance(request: Request):
    """Lazy-init the :class:`ComplianceStore` (test-friendly)."""

    from .compliance import ComplianceStore

    store = getattr(request.app.state, "compliance", None)
    if store is None:
        store = ComplianceStore(request.app.state.settings.db_path)
        request.app.state.compliance = store
    return store


ManagerDep = Annotated[TokenManager, Depends(_get_manager)]
StoreDep = Annotated[TokenStore, Depends(_get_store)]
TenantsDep = Annotated[TenantStore, Depends(_get_tenants)]
QuotasDep = Annotated[QuotaStore, Depends(_get_quotas)]


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

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics_endpoint(request: Request):
        """Prometheus exposition endpoint.

        Refreshes the active-tokens gauge on each scrape from the in-memory
        revocation cache + token store. Best-effort: any failure leaves
        the previously-set value in place.
        """

        registry: MetricsRegistry = request.app.state.metrics
        try:
            await _refresh_identity_gauges(request.app, registry)
        except Exception:  # noqa: BLE001 — never crash a scrape
            pass
        return metrics_response(registry)

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

    # ------------------------------------------------------------------ regions

    @app.get(
        "/v1/regions",
        response_model=RegionsResponse,
        tags=["meta"],
    )
    async def get_regions(request: Request) -> RegionsResponse:
        """Return the current region + cached peer reachability.

        Identity's cross-region propagation lever — token revocation
        polling — is wired separately (see ``revocation.py``); this
        endpoint exposes peer reachability for the discovery surface.
        """

        settings: Settings = request.app.state.settings
        probe: RegionStatusProbe = request.app.state.region_status_probe
        peer_urls = {
            peer_id: settings.region_peer_urls.get(peer_id, "")
            for peer_id in settings.region_peers
            if settings.region_peer_urls.get(peer_id)
        }
        peers = await probe.all_peers(peer_urls) if peer_urls else []
        return RegionsResponse(
            current=settings.region_id,
            mode=settings.replication_mode,
            peers=list(peers),
        )

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
        try:
            request.app.state.metrics.counter(
                "plinth_tokens_issued_total",
                {"tenant_id": str(claims.tenant_id or "default")},
            ).inc(1)
        except Exception:  # noqa: BLE001 — metrics never crash core path
            pass
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
        request: Request,
    ) -> TokenClaims:
        if not body.token:
            raise InvalidArguments(
                "token is required",
                details={"field": "token"},
            )

        try:
            claims = await manager.decode_async(body.token)
        except TokenExpired:
            _bump_verify_counter(request.app, "expired")
            raise
        except InvalidToken:
            _bump_verify_counter(request.app, "invalid")
            raise

        if await store.is_revoked(claims.jti):
            _bump_verify_counter(request.app, "revoked")
            raise TokenRevoked(
                f"Token {claims.jti} has been revoked",
                details={"jti": claims.jti},
            )

        _bump_verify_counter(request.app, "ok")
        return claims

    @app.post(
        "/v1/tokens/{jti}/revoke",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["tokens"],
    )
    async def revoke_token(
        jti: str,
        store: StoreDep,
        request: Request,
    ) -> Response:
        await store.revoke(jti)
        get_logger().info("identity.token.revoked", jti=jti)
        try:
            request.app.state.metrics.counter(
                "plinth_tokens_revoked_total",
                {"service": __service__},
            ).inc(1)
        except Exception:  # noqa: BLE001 — metrics never crash
            pass
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

    # ----------------------------------------------------------- quotas (v1.0)
    #
    # Per-tenant resource quota envelope. The endpoints are additive: tenants
    # without an explicit row return defaults so existing callers see no
    # change. Workspace + Gateway poll ``GET .../quotas`` (cached locally
    # for 60s) before accepting create/invoke calls. See ``CONTRACTS.md`` —
    # "Per-Tenant Resource Quotas".

    @app.get(
        "/v1/tenants/{tenant_id}/quotas",
        response_model=TenantQuotas,
        tags=["tenants"],
    )
    async def get_tenant_quotas(
        tenant_id: str,
        quotas: QuotasDep,
    ) -> TenantQuotas:
        # Per spec: a tenant with no row returns defaults (never 404).
        # Whether the tenant *itself* exists is checked downstream — the
        # quota endpoint is intentionally permissive so caller services
        # can fetch quotas from a still-bootstrapping deployment.
        return await quotas.get(tenant_id)

    @app.post(
        "/v1/tenants/{tenant_id}/quotas",
        response_model=TenantQuotas,
        tags=["tenants"],
    )
    async def set_tenant_quotas(
        tenant_id: str,
        body: TenantQuotasUpdate,
        quotas: QuotasDep,
    ) -> TenantQuotas:
        result = await quotas.set(tenant_id, body)
        get_logger().info(
            "identity.tenant.quotas.set",
            tenant_id=tenant_id,
            max_workspaces=result.max_workspaces,
            max_storage_gb=result.max_storage_gb,
            max_cost_usd_day=result.max_cost_usd_day,
        )
        return result

    @app.delete(
        "/v1/tenants/{tenant_id}/quotas",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["tenants"],
    )
    async def reset_tenant_quotas(
        tenant_id: str,
        quotas: QuotasDep,
    ) -> Response:
        await quotas.delete(tenant_id)
        get_logger().info("identity.tenant.quotas.reset", tenant_id=tenant_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/tenants/{tenant_id}/usage",
        response_model=TenantUsage,
        tags=["tenants"],
    )
    async def get_tenant_usage(
        tenant_id: str,
        quotas: QuotasDep,
    ) -> TenantUsage:
        return await quotas.usage(tenant_id)

    # ----------------------------------------------------- v1.0 GDPR — exports

    @app.post(
        "/v1/tenants/{tenant_id}/export",
        response_model=ExportJobAcknowledgement,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["compliance"],
    )
    async def request_tenant_export(
        tenant_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        tenants: TenantsDep,
    ) -> ExportJobAcknowledgement:
        """Kick off a GDPR Article 20 (data portability) export.

        Returns 202 with a pending ``export_id``. The client polls
        ``GET /v1/tenants/{id}/exports/{export_id}`` until ``status``
        becomes ``ready``, then ``GET .../download`` for the ZIP.
        """

        from .compliance import run_export

        await tenants.get(tenant_id)  # 404 if missing
        store = _get_compliance(request)
        export = await store.create_export(tenant_id)

        gw_settings: Settings = request.app.state.settings
        exports_dir = gw_settings.data_dir / "exports"
        background_tasks.add_task(
            run_export,
            store=store,
            export_id=export.export_id,
            tenant_id=tenant_id,
            workspace_url=gw_settings.workspace_url or None,
            gateway_url=gw_settings.gateway_url or None,
            exports_dir=exports_dir,
            db_path=gw_settings.db_path,
        )
        get_logger().info(
            "identity.compliance.export.requested",
            export_id=export.export_id,
            tenant_id=tenant_id,
        )
        return ExportJobAcknowledgement(
            export_id=export.export_id,
            status="pending",
        )

    @app.get(
        "/v1/tenants/{tenant_id}/exports/{export_id}",
        response_model=ExportStatus,
        tags=["compliance"],
    )
    async def get_export_status(
        tenant_id: str,
        export_id: str,
        request: Request,
    ) -> ExportStatus:
        store = _get_compliance(request)
        export = await store.get_export(export_id)
        if export is None or export.tenant_id != tenant_id:
            return JSONResponse(  # type: ignore[return-value]
                status_code=404,
                content={
                    "error": {
                        "code": "EXPORT_NOT_FOUND",
                        "message": f"export {export_id!r} not found",
                        "details": {"export_id": export_id},
                    }
                },
            )
        # Late expiry: surface "expired" once we're past expires_at.
        if (
            export.expires_at is not None
            and export.status == "ready"
            and export.expires_at < datetime.now(UTC).replace(microsecond=0)
        ):
            await store.update_export(
                export_id,
                status="expired",
                completed_at=export.completed_at,
                expires_at=export.expires_at,
                size_bytes=export.size_bytes,
            )
            export = await store.get_export(export_id)
            assert export is not None  # noqa: S101
        return export

    @app.get(
        "/v1/tenants/{tenant_id}/exports/{export_id}/download",
        tags=["compliance"],
    )
    async def download_export(
        tenant_id: str,
        export_id: str,
        request: Request,
    ) -> Response:
        store = _get_compliance(request)
        export = await store.get_export(export_id)
        if export is None or export.tenant_id != tenant_id:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "EXPORT_NOT_FOUND",
                        "message": f"export {export_id!r} not found",
                        "details": {"export_id": export_id},
                    }
                },
            )
        if export.status != "ready":
            if (
                export.status == "ready"
                or export.status == "expired"
                or (
                    export.expires_at is not None
                    and export.expires_at
                    < datetime.now(UTC).replace(microsecond=0)
                )
            ):
                return JSONResponse(
                    status_code=410,
                    content={
                        "error": {
                            "code": "EXPORT_EXPIRED",
                            "message": "export has expired",
                            "details": {"export_id": export_id},
                        }
                    },
                )
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "EXPORT_NOT_READY",
                        "message": (
                            f"export {export_id!r} is in status "
                            f"{export.status!r}; not yet ready"
                        ),
                        "details": {"status": export.status},
                    }
                },
            )
        if (
            export.expires_at is not None
            and export.expires_at < datetime.now(UTC).replace(microsecond=0)
        ):
            return JSONResponse(
                status_code=410,
                content={
                    "error": {
                        "code": "EXPORT_EXPIRED",
                        "message": "export has expired",
                        "details": {"export_id": export_id},
                    }
                },
            )
        gw_settings: Settings = request.app.state.settings
        exports_dir = gw_settings.data_dir / "exports"
        zip_path = exports_dir / f"{export_id}.zip"
        if not zip_path.exists():
            return JSONResponse(
                status_code=410,
                content={
                    "error": {
                        "code": "EXPORT_FILE_MISSING",
                        "message": "export file no longer present on disk",
                        "details": {"export_id": export_id},
                    }
                },
            )
        return FileResponse(
            str(zip_path),
            media_type="application/zip",
            filename=f"plinth-export-{tenant_id}-{export_id}.zip",
        )

    # ----------------------------------------------------- v1.0 GDPR — deletes

    @app.post(
        "/v1/tenants/{tenant_id}/delete-data-confirm",
        response_model=DeleteConfirmation,
        tags=["compliance"],
    )
    async def request_delete_confirmation(
        tenant_id: str,
        request: Request,
        tenants: TenantsDep,
    ) -> DeleteConfirmation:
        """Phase 1 of GDPR Article 17 erasure — issue a one-shot confirm token.

        The token is short-lived (10 min). The caller passes it back as
        ``?confirm=<token>`` on the actual ``DELETE`` to prove intent and
        protect against accidental cascade.
        """

        await tenants.get(tenant_id)
        store = _get_compliance(request)
        token, expires_at = await store.issue_confirm_token(tenant_id)
        get_logger().info(
            "identity.compliance.delete.confirm_issued",
            tenant_id=tenant_id,
        )
        return DeleteConfirmation(confirm_token=token, expires_at=expires_at)

    @app.delete(
        "/v1/tenants/{tenant_id}/data",
        response_model=DeleteJob,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["compliance"],
    )
    async def delete_tenant_data(
        tenant_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        tenants: TenantsDep,
        confirm: Annotated[str, Query(min_length=1)],
    ) -> DeleteJob:
        """Phase 2 of GDPR Article 17 erasure — kick off the cascade."""

        from .compliance import run_delete

        await tenants.get(tenant_id)
        store = _get_compliance(request)
        ok = await store.consume_confirm_token(confirm, tenant_id)
        if not ok:
            return JSONResponse(  # type: ignore[return-value]
                status_code=400,
                content={
                    "error": {
                        "code": "DELETE_CONFIRM_INVALID",
                        "message": (
                            "confirm token missing, expired, or wrong tenant"
                        ),
                        "details": {"tenant_id": tenant_id},
                    }
                },
            )
        job = await store.create_delete_job(tenant_id)
        gw_settings: Settings = request.app.state.settings
        background_tasks.add_task(
            run_delete,
            store=store,
            job_id=job.job_id,
            tenant_id=tenant_id,
            workspace_url=gw_settings.workspace_url or None,
            gateway_url=gw_settings.gateway_url or None,
            db_path=gw_settings.db_path,
        )
        get_logger().info(
            "identity.compliance.delete.requested",
            job_id=job.job_id,
            tenant_id=tenant_id,
        )
        return job

    @app.get(
        "/v1/tenants/{tenant_id}/delete-jobs/{job_id}",
        response_model=DeleteJob,
        tags=["compliance"],
    )
    async def get_delete_job_status(
        tenant_id: str,
        job_id: str,
        request: Request,
    ) -> DeleteJob:
        store = _get_compliance(request)
        job = await store.get_delete_job(job_id)
        if job is None or job.tenant_id != tenant_id:
            return JSONResponse(  # type: ignore[return-value]
                status_code=404,
                content={
                    "error": {
                        "code": "DELETE_JOB_NOT_FOUND",
                        "message": f"job {job_id!r} not found",
                        "details": {"job_id": job_id},
                    }
                },
            )
        return job

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


def _bump_verify_counter(app: FastAPI, result: str) -> None:
    """Bump the per-result token-verification counter (best-effort)."""

    try:
        registry: MetricsRegistry | None = getattr(app.state, "metrics", None)
        if registry is None:
            return
        registry.counter(
            "plinth_token_verifications_total",
            {"result": str(result)},
        ).inc(1)
    except Exception:  # noqa: BLE001
        pass


async def _refresh_identity_gauges(app: FastAPI, registry: MetricsRegistry) -> None:
    """Refresh identity-specific gauges on each Prometheus scrape.

    Best-effort: any failure leaves the previously-set value in place. The
    ``tokens_active`` count is the difference between issued and revoked
    rows (computed via the existing token store).
    """

    store = getattr(app.state, "store", None)
    if store is None:
        return
    count_fn = getattr(store, "count_active", None)
    if callable(count_fn):
        try:
            n = await count_fn()
            registry.gauge(
                "plinth_tokens_active",
                {"service": __service__},
            ).set(int(n or 0))
        except Exception:  # noqa: BLE001
            pass


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
