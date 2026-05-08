# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app + routes for the Plinth Tool Gateway."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .audit import AuditLog, AuditRecord
from .auth import check_inbound_auth
from .cache import Cache, hash_args, hash_result
from .db import Database
from .encryption import load_or_generate_key
from .exceptions import (
    CostCapExceeded,
    GatewayError,
    InvalidArguments,
    RateLimited,
    ToolNotFound,
    Unauthorized,
)
from .jwt_auth import extract_auth_context_async
from .limits import LimitsRegistry
from .load_shed import LoadShedder, load_shed_middleware
from .logging_config import configure_logging, get_logger
from .migration_runner import (
    MigrationLockError,
    MigrationRollbackMissing as RunnerRollbackMissing,
    MigrationRunner,
    default_migrations_dir,
)
from .models import (
    AgentLimits,
    AgentLimitsBody,
    AuditListResponse,
    AuditStatsResponse,
    CacheStats,
    DryRunResponse,
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    InvokeRequest,
    InvokeResponse,
    LimitsStatus,
    RollbackBody,
    RollbackResult,
    RolledBackMigrationModel,
    Tenant,
    TenantList,
    Tool,
    ToolListResponse,
    ToolRegistration,
)
from .oauth import OAuthConnectionStore, OAuthStateStore
from .oauth_api import create_oauth_router
from .otlp_api import create_otlp_router
from .otlp_emitter import OTLPEmitter
from .policy import check_capability
from .pricing import estimate_cost
from .proxy import HttpProxy
from .registry import Registry
from .revocation_cache import RevocationCache
from .settings import Settings, get_settings
from .transactions_api import create_transactions_router


def _error_payload(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    return ErrorResponse(
        error=ErrorBody(code=code, message=message, details=details or {})
    ).model_dump()


def _scope_tenant(request: Request) -> str | None:
    """Return the tenant filter for list/read endpoints.

    In ``permissive`` mode every request lands in tenant ``default`` and we
    DO NOT filter — all data is visible to all callers (so v0.2 demos keep
    working). In ``verify_local`` / ``verify_remote`` we filter strictly.
    """

    settings: Settings = request.app.state.settings
    if settings.auth_mode == "permissive":
        return None
    return getattr(request.state, "tenant_id", "default")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct a FastAPI app instance.

    A factory keeps tests isolated — each test can pass a custom Settings and
    wire up its own SQLite path / httpx client. The lifespan handler creates
    the Database, Registry, Cache, AuditLog, and HttpProxy and stashes them
    in ``app.state``.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_data_dir()

        # Surface the auth mode at startup. Same shape as the workspace's
        # warning so operators immediately notice when a deploy isn't using
        # JWTs.
        if settings.auth_mode == "permissive" and not settings.identity_jwt_secret:
            log.warning(
                "gateway.auth.disabled",
                hint=(
                    "AUTH DISABLED: every request lands in tenant 'default'. "
                    "Set PLINTH_AUTH_MODE=verify_local + "
                    "PLINTH_IDENTITY_JWT_SECRET to enforce JWTs."
                ),
            )
        elif settings.auth_mode == "verify_local" and not settings.identity_jwt_secret:
            # An RS256 deployment doesn't need a shared secret — the
            # verifier resolves keys via JWKS. We only fail closed when
            # there's neither a secret nor a path to JWKS-based RS256
            # verification (the identity URL is required for that).
            if not settings.identity_url:
                raise RuntimeError(
                    "PLINTH_AUTH_MODE=verify_local requires either "
                    "PLINTH_IDENTITY_JWT_SECRET (HS256) or "
                    "PLINTH_IDENTITY_URL (RS256 via JWKS)",
                )

        db = Database(settings.db_path)
        await db.connect()

        # v0.5 — schema migrations. Apply pending migrations after the
        # legacy CREATE-IF-NOT-EXISTS bootstrap (``Database.connect``) so
        # existing v0.1–v0.4 databases get marked-as-applied without
        # re-running SQL. When ``auto_migrate=False`` we still log the
        # pending list so operators see it on boot. ``database_url``
        # selects the v0.6 Postgres advisory-lock path; empty keeps SQLite.
        runner = MigrationRunner(
            settings.db_path,
            default_migrations_dir(__file__),
            database_url=settings.effective_database_url,
            service_name="gateway",
        )
        app.state.migration_runner = runner
        try:
            if settings.auto_migrate:
                applied_migs = await runner.apply_pending(blocking_lock=True)
                if applied_migs:
                    log.info(
                        "gateway.migrations.applied",
                        count=len(applied_migs),
                        ids=[a.id for a in applied_migs],
                    )
            else:
                pending_migs = await runner.list_pending()
                if pending_migs:
                    log.warning(
                        "gateway.migrations.pending",
                        count=len(pending_migs),
                        ids=[m.id for m in pending_migs],
                        hint=(
                            "auto_migrate is disabled. Run "
                            "`python -m plinth_gateway migrate` to apply."
                        ),
                    )
        except MigrationLockError as exc:
            log.warning("gateway.migrations.locked", error=str(exc))

        cache = Cache(db)
        cleared = await cache.cleanup_expired()
        if cleared:
            log.info("cache.cleanup_on_startup", entries=cleared)

        proxy = HttpProxy(timeout_seconds=settings.backend_timeout_seconds)

        # OTLP emitter: best-effort log forwarder for audit events. Constructed
        # unconditionally; ``start`` is a no-op when ``otlp_enabled=False`` so
        # back-compat with v0.3 deploys is preserved exactly.
        otlp = OTLPEmitter(settings)
        await otlp.start()

        # OAuth: load (or auto-generate in dev) the at-rest encryption key.
        # This never crashes on startup — if the key is unconfigured and we're
        # in dev mode we generate one and warn. The OAuth provider credentials
        # may also be empty; we don't crash on those either, but the relevant
        # endpoints will return 503 with a helpful message.
        encryption_key = load_or_generate_key(
            settings.oauth_encryption_key,
            data_dir=settings.data_dir,
        )

        app.state.settings = settings
        app.state.db = db
        app.state.registry = Registry(db)
        app.state.cache = cache
        app.state.otlp = otlp
        app.state.audit = AuditLog(db, otlp=otlp)
        app.state.proxy = proxy
        app.state.limits = LimitsRegistry(db, settings)
        app.state.oauth_encryption_key = encryption_key
        app.state.oauth_connections = OAuthConnectionStore(
            db, encryption_key=encryption_key
        )
        app.state.oauth_states = OAuthStateStore(
            db, ttl_seconds=settings.oauth_state_ttl_seconds
        )

        # v0.5 — load shedding. Always constructed so the admin/stats endpoint
        # has something to report; ``enabled=False`` makes the middleware a
        # no-op (back-compat for v0.4 deployments).
        app.state.load_shedder = LoadShedder(
            max_inflight=settings.load_shed_max_inflight,
            max_queue=settings.load_shed_max_queue,
            retry_after_seconds=settings.load_shed_retry_after_seconds,
            enabled=settings.load_shed_enabled,
        )

        # v0.6 — federated revocation cache. Constructed eagerly so the
        # auth middleware + admin/stats endpoint can rely on its presence.
        # The polling loop is started below only when the URL is set.
        rev_cache = RevocationCache(
            identity_url=settings.revocation_poll_url,
            poll_interval=settings.revocation_poll_interval_seconds,
        )
        app.state.revocation_cache = rev_cache
        if (
            settings.revocation_poll_url
            and settings.revocation_poll_enabled
        ):
            try:
                await rev_cache.start()
                log.info(
                    "gateway.revocation_cache.started",
                    identity_url=rev_cache.identity_url,
                    poll_interval=rev_cache.poll_interval,
                    initial_size=rev_cache.stats["size"],
                )
            except Exception as exc:  # noqa: BLE001 - never break startup
                log.warning(
                    "gateway.revocation_cache.start_failed",
                    error=str(exc),
                )

        log.info(
            "gateway.startup",
            db_path=str(settings.db_path),
            port=settings.gateway_port,
        )
        try:
            yield
        finally:
            # Flush + shut down OTLP first so any pending events make it out
            # before we tear down the rest of the dependency graph.
            await otlp.stop()
            await proxy.aclose()
            try:
                await rev_cache.stop()
            except Exception as exc:  # noqa: BLE001 - never break shutdown
                log.warning(
                    "gateway.revocation_cache.stop_failed",
                    error=str(exc),
                )
            await db.close()
            log.info("gateway.shutdown")

    app = FastAPI(
        title="Plinth Tool Gateway",
        version=__version__,
        description="Agent-native HTTP/MCP tool proxy with caching, audit, and dry-run.",
        lifespan=lifespan,
    )

    # ---- middleware: extract tenant_id + agent_id from JWT -----------------

    @app.middleware("http")
    async def _tenant_context_middleware(request: Request, call_next):
        """Populate ``request.state.tenant_id`` + ``request.state.agent_id``.

        Runs in every mode. ``permissive`` (default) keeps everything in
        tenant ``"default"`` so v0.2 demos see no behaviour change.
        """

        request.state.tenant_id = "default"
        request.state.agent_id = None
        request.state.auth_scopes = []

        if request.url.path == "/healthz":
            return await call_next(request)

        gw_settings: Settings = app.state.settings
        try:
            ctx = await extract_auth_context_async(
                request.headers.get("authorization"),
                gw_settings,
            )
        except Unauthorized as exc:
            return JSONResponse(
                status_code=exc.http_status,
                content=_error_payload(exc.code, exc.message, exc.details),
            )

        # v0.6 — federated revocation. After a successful JWT decode, check
        # the in-memory blocklist (populated by polling Identity). The cache
        # is only consulted in non-permissive auth modes — permissive mode
        # generally has no JTI to check anyway.
        if (
            gw_settings.auth_mode in ("verify_local", "verify_remote")
            and ctx.authenticated
            and ctx.jti is not None
        ):
            rev_cache: RevocationCache | None = getattr(
                app.state, "revocation_cache", None
            )
            if rev_cache is not None and rev_cache.is_revoked(ctx.jti):
                return JSONResponse(
                    status_code=401,
                    content=_error_payload(
                        "TOKEN_REVOKED",
                        "token has been revoked",
                        {"jti": ctx.jti},
                    ),
                )

        request.state.tenant_id = ctx.tenant_id
        request.state.agent_id = ctx.agent_id
        request.state.auth_scopes = ctx.scopes or []
        return await call_next(request)

    # ---- v0.5 load-shed middleware (outermost) -----------------------------

    # Order: middleware added LAST runs FIRST. Registering load-shed after
    # the tenant context means a 503 short-circuits before we touch auth or
    # any downstream state (cheap rejection under overload).
    app.middleware("http")(load_shed_middleware)

    # ---- exception handlers ------------------------------------------------

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        headers: dict[str, str] = {}
        # Both rate-limit and cost-cap errors come back as 429; surface the
        # standard Retry-After header so clients (and the SDK) can back off.
        if isinstance(exc, (RateLimited, CostCapExceeded)):
            retry_after = getattr(exc, "retry_after", 0.0)
            try:
                # HTTP/1.1 says Retry-After is delta-seconds (integer).
                # Round up so we never recommend retrying *before* the bucket
                # would actually be ready.
                from math import ceil

                headers["Retry-After"] = str(max(1, ceil(float(retry_after))))
            except (TypeError, ValueError, OverflowError):
                headers["Retry-After"] = "60"
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_payload(exc.code, exc.message, exc.details),
            headers=headers or None,
        )

    # ---- dependencies ------------------------------------------------------

    async def auth_dep(authorization: str | None = Header(default=None)) -> None:
        if not app.state.settings.inbound_auth_required:
            return
        check_inbound_auth(authorization)

    def get_registry() -> Registry:
        return app.state.registry

    def get_cache() -> Cache:
        return app.state.cache

    def get_audit() -> AuditLog:
        return app.state.audit

    def get_proxy() -> HttpProxy:
        return app.state.proxy

    def get_limits() -> LimitsRegistry:
        return app.state.limits

    # ---- health ------------------------------------------------------------

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, service="gateway")

    # ---- tools -------------------------------------------------------------

    @app.post(
        "/v1/tools/register",
        response_model=Tool,
        status_code=201,
        tags=["tools"],
        dependencies=[Depends(auth_dep)],
    )
    async def register_tool(
        payload: ToolRegistration,
        request: Request,
        registry: Registry = Depends(get_registry),
    ) -> Tool:
        tenant_id = getattr(request.state, "tenant_id", "default")
        return await registry.register(payload, tenant_id=tenant_id)

    @app.get(
        "/v1/tools",
        response_model=ToolListResponse,
        tags=["tools"],
        dependencies=[Depends(auth_dep)],
    )
    async def list_tools(
        request: Request,
        registry: Registry = Depends(get_registry),
    ) -> ToolListResponse:
        tenant_id = _scope_tenant(request)
        return ToolListResponse(tools=await registry.list(tenant_id=tenant_id))

    @app.get(
        "/v1/tenants",
        response_model=TenantList,
        tags=["tools"],
        dependencies=[Depends(auth_dep)],
    )
    async def list_tenants(
        audit: AuditLog = Depends(get_audit),
    ) -> TenantList:
        rows = await audit.list_tenants()
        return TenantList(tenants=[Tenant(**row) for row in rows])

    @app.get(
        "/v1/tools/{tool_id}",
        response_model=Tool,
        tags=["tools"],
        dependencies=[Depends(auth_dep)],
    )
    async def get_tool(
        tool_id: str,
        request: Request,
        registry: Registry = Depends(get_registry),
    ) -> Tool:
        tenant_id = _scope_tenant(request)
        return await registry.get(tool_id, tenant_id=tenant_id)

    @app.delete(
        "/v1/tools/{tool_id}",
        status_code=204,
        tags=["tools"],
        dependencies=[Depends(auth_dep)],
    )
    async def delete_tool(
        tool_id: str,
        request: Request,
        registry: Registry = Depends(get_registry),
    ) -> Response:
        # Tenant-scoped visibility check first; raises ToolNotFound on mismatch.
        tenant_id = _scope_tenant(request)
        if tenant_id is not None:
            await registry.get(tool_id, tenant_id=tenant_id)
        await registry.delete(tool_id)
        return Response(status_code=204)

    # ---- invoke ------------------------------------------------------------

    @app.post(
        "/v1/invoke",
        response_model=InvokeResponse,
        tags=["invoke"],
        dependencies=[Depends(auth_dep)],
    )
    async def invoke(
        payload: InvokeRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        registry: Registry = Depends(get_registry),
        cache: Cache = Depends(get_cache),
        audit: AuditLog = Depends(get_audit),
        proxy: HttpProxy = Depends(get_proxy),
        limits: LimitsRegistry = Depends(get_limits),
    ) -> InvokeResponse:
        # Rate-limit + cost-cap enforcement.
        # Per CONTRACTS.md (v0.2 Additions: "Rate Limiting & Cost Caps"), limits
        # are enforced on identified agent traffic only. Anonymous calls (no
        # agent_id) skip enforcement in v0.2 — when we add OAuth-issued scoped
        # tokens we'll require an agent_id and can revisit. The gateway-wide
        # ``rate_limits_enabled`` setting also short-circuits enforcement (used
        # for benchmarks and local dev).
        settings = app.state.settings
        tenant_id = getattr(request.state, "tenant_id", "default")
        if settings.rate_limits_enabled and payload.agent_id is not None:
            await limits.assert_within_rate(payload.agent_id)
            await limits.assert_within_cost_caps(payload.agent_id)

        # In strict-auth modes, scope tool lookup to the caller's tenant so
        # tenant A can't invoke tools registered in tenant B.
        scope_tenant = _scope_tenant(request)
        tool = await registry.get(payload.tool_id, tenant_id=scope_tenant)

        check_capability(
            tool_id=tool.tool_id,
            workspace_id=payload.workspace_id,
            agent_id=payload.agent_id,
            token=authorization,
        )

        args_hash = hash_args(payload.arguments)
        args_preview = AuditLog.make_preview(payload.arguments)

        cache_eligible = (
            payload.cache
            and tool.idempotent
            and tool.cache_ttl_seconds is not None
            and tool.cache_ttl_seconds > 0
        )

        # 1) Cache lookup
        if cache_eligible:
            hit = await cache.lookup(tool.tool_id, payload.arguments)
            if hit is not None:
                duration_ms = 0
                cost = estimate_cost(tool.tool_id, cached=True)
                event = await audit.record(
                    AuditRecord(
                        tool_id=tool.tool_id,
                        arguments=payload.arguments,
                        workspace_id=payload.workspace_id,
                        agent_id=payload.agent_id,
                        tenant_id=tenant_id,
                        arguments_hash=args_hash,
                        arguments_preview=args_preview,
                        cached=True,
                        duration_ms=duration_ms,
                        cost_estimate_usd=cost,
                        result_hash=hash_result(hit.result),
                    )
                )
                log.info(
                    "invoke.cache_hit",
                    tool_id=tool.tool_id,
                    workspace_id=payload.workspace_id,
                    audit_id=event.id,
                )
                return InvokeResponse(
                    tool_id=tool.tool_id,
                    arguments=payload.arguments,
                    result=hit.result,
                    cached=True,
                    duration_ms=duration_ms,
                    audit_id=event.id,
                    cost_estimate_usd=cost,
                )

        # 2) Backend call
        start = time.perf_counter()
        error_message: str | None = None
        result: Any = None
        try:
            result = await proxy.invoke(
                tool,
                payload.arguments,
                connection_store=app.state.oauth_connections,
                settings=app.state.settings,
            )
        except GatewayError as exc:
            error_message = exc.message
            duration_ms = int((time.perf_counter() - start) * 1000)
            cost = estimate_cost(tool.tool_id, cached=False)
            event = await audit.record(
                AuditRecord(
                    tool_id=tool.tool_id,
                    arguments=payload.arguments,
                    workspace_id=payload.workspace_id,
                    agent_id=payload.agent_id,
                    tenant_id=tenant_id,
                    arguments_hash=args_hash,
                    arguments_preview=args_preview,
                    cached=False,
                    duration_ms=duration_ms,
                    cost_estimate_usd=cost,
                    error=error_message,
                )
            )
            log.warning(
                "invoke.error",
                tool_id=tool.tool_id,
                workspace_id=payload.workspace_id,
                audit_id=event.id,
                error=error_message,
            )
            # Re-raise with the audit_id surfaced in details so clients can find it.
            raise GatewayError(
                exc.message,
                code=exc.code,
                http_status=exc.http_status,
                details={**exc.details, "audit_id": event.id},
            ) from exc

        duration_ms = int((time.perf_counter() - start) * 1000)
        cost = estimate_cost(tool.tool_id, cached=False)

        # 3) Store cache (only if eligible AND payload.cache=True)
        if cache_eligible:
            await cache.store(
                tool.tool_id,
                payload.arguments,
                result,
                ttl_seconds=tool.cache_ttl_seconds or 0,
            )

        # 4) Audit
        event = await audit.record(
            AuditRecord(
                tool_id=tool.tool_id,
                arguments=payload.arguments,
                workspace_id=payload.workspace_id,
                agent_id=payload.agent_id,
                tenant_id=tenant_id,
                arguments_hash=args_hash,
                arguments_preview=args_preview,
                cached=False,
                duration_ms=duration_ms,
                cost_estimate_usd=cost,
                result_hash=hash_result(result),
            )
        )
        log.info(
            "invoke.success",
            tool_id=tool.tool_id,
            workspace_id=payload.workspace_id,
            audit_id=event.id,
            duration_ms=duration_ms,
        )

        return InvokeResponse(
            tool_id=tool.tool_id,
            arguments=payload.arguments,
            result=result,
            cached=False,
            duration_ms=duration_ms,
            audit_id=event.id,
            cost_estimate_usd=cost,
        )

    @app.post(
        "/v1/invoke/dry-run",
        response_model=DryRunResponse,
        tags=["invoke"],
        dependencies=[Depends(auth_dep)],
    )
    async def dry_run(
        payload: InvokeRequest,
        request: Request,
        registry: Registry = Depends(get_registry),
        cache: Cache = Depends(get_cache),
    ) -> DryRunResponse:
        tenant_id = _scope_tenant(request)
        tool = await registry.get(payload.tool_id, tenant_id=tenant_id)
        cache_eligible = (
            payload.cache
            and tool.idempotent
            and tool.cache_ttl_seconds is not None
            and tool.cache_ttl_seconds > 0
        )
        cached_result: Any | None = None
        would_invoke = True
        if cache_eligible:
            hit = await cache.lookup(tool.tool_id, payload.arguments)
            if hit is not None:
                cached_result = hit.result
                would_invoke = False

        cost = estimate_cost(tool.tool_id, cached=not would_invoke)
        return DryRunResponse(
            tool_id=tool.tool_id,
            arguments=payload.arguments,
            would_invoke=would_invoke,
            cached_result=cached_result,
            estimated_cost_usd=cost,
            estimated_duration_ms=0 if not would_invoke else 100,
        )

    # ---- audit -------------------------------------------------------------

    @app.get(
        "/v1/audit",
        response_model=AuditListResponse,
        tags=["audit"],
        dependencies=[Depends(auth_dep)],
    )
    async def list_audit(
        request: Request,
        workspace_id: str | None = Query(default=None),
        tool_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        since: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        audit: AuditLog = Depends(get_audit),
    ) -> AuditListResponse:
        since_dt: datetime | None = None
        if since is not None:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidArguments(
                    f"Invalid 'since' timestamp: {since!r}",
                    details={"since": since},
                ) from exc
        events = await audit.query(
            workspace_id=workspace_id,
            tool_id=tool_id,
            agent_id=agent_id,
            tenant_id=_scope_tenant(request),
            since=since_dt,
            limit=limit,
        )
        return AuditListResponse(events=events)

    @app.get(
        "/v1/audit/stats",
        response_model=AuditStatsResponse,
        tags=["audit"],
        dependencies=[Depends(auth_dep)],
    )
    async def audit_stats(
        request: Request,
        workspace_id: str | None = Query(default=None),
        audit: AuditLog = Depends(get_audit),
    ) -> AuditStatsResponse:
        stats = await audit.stats(
            workspace_id=workspace_id,
            tenant_id=_scope_tenant(request),
        )
        return AuditStatsResponse(stats=stats)

    # ---- cache -------------------------------------------------------------

    @app.get(
        "/v1/cache/stats",
        response_model=CacheStats,
        tags=["cache"],
        dependencies=[Depends(auth_dep)],
    )
    async def cache_stats(
        cache: Cache = Depends(get_cache),
    ) -> CacheStats:
        s = await cache.stats()
        return CacheStats(**s)

    @app.delete(
        "/v1/cache",
        status_code=204,
        tags=["cache"],
        dependencies=[Depends(auth_dep)],
    )
    async def clear_cache(
        tool_id: str | None = Query(default=None),
        registry: Registry = Depends(get_registry),
        cache: Cache = Depends(get_cache),
    ) -> Response:
        if tool_id is not None:
            # Verify the tool exists so we 404 cleanly when mistyped.
            tool = await registry.get_optional(tool_id)
            if tool is None:
                raise ToolNotFound(
                    f"Tool {tool_id!r} is not registered",
                    details={"tool_id": tool_id},
                )
        await cache.clear(tool_id)
        return Response(status_code=204)

    # ---- limits ------------------------------------------------------------

    @app.get(
        "/v1/limits/{agent_id}",
        response_model=AgentLimits,
        tags=["limits"],
        dependencies=[Depends(auth_dep)],
    )
    async def get_agent_limits(
        agent_id: str,
        limits: LimitsRegistry = Depends(get_limits),
    ) -> AgentLimits:
        return await limits.get_limits(agent_id)

    @app.post(
        "/v1/limits/{agent_id}",
        response_model=AgentLimits,
        tags=["limits"],
        dependencies=[Depends(auth_dep)],
    )
    async def set_agent_limits(
        agent_id: str,
        body: AgentLimitsBody,
        limits: LimitsRegistry = Depends(get_limits),
    ) -> AgentLimits:
        return await limits.set_limits(agent_id, body)

    @app.delete(
        "/v1/limits/{agent_id}",
        status_code=204,
        tags=["limits"],
        dependencies=[Depends(auth_dep)],
    )
    async def delete_agent_limits(
        agent_id: str,
        limits: LimitsRegistry = Depends(get_limits),
    ) -> Response:
        await limits.delete_limits(agent_id)
        # 204 whether or not a row was present — DELETE is idempotent.
        return Response(status_code=204)

    @app.get(
        "/v1/limits/{agent_id}/status",
        response_model=LimitsStatus,
        tags=["limits"],
        dependencies=[Depends(auth_dep)],
    )
    async def agent_limits_status(
        agent_id: str,
        limits: LimitsRegistry = Depends(get_limits),
    ) -> LimitsStatus:
        cfg = await limits.get_limits(agent_id)
        # ``rpm_used_in_window`` answers "how full is the bucket right now?".
        # If the agent has a live bucket, derive from it (capacity − tokens).
        # Otherwise fall back to the audit-log count over the last 60s, which
        # is what new agents see before they make their first call.
        bucket_entry = await limits.rate_limiter.get_bucket(agent_id)
        if bucket_entry is not None:
            tokens = bucket_entry.bucket.snapshot_tokens()
            rpm_used = max(0, int(round(bucket_entry.burst - tokens)))
        else:
            rpm_used = await limits.rpm_used(agent_id)
        used_hour = await limits.cost_used(agent_id, 1)
        used_day = await limits.cost_used(agent_id, 24)
        return LimitsStatus(
            agent_id=agent_id,
            rpm_limit=cfg.rpm,
            rpm_used_in_window=rpm_used,
            cost_cap_usd_hour=cfg.cost_cap_usd_hour,
            cost_used_usd_hour=used_hour,
            cost_cap_usd_day=cfg.cost_cap_usd_day,
            cost_used_usd_day=used_day,
        )

    # ---- OAuth router (additive — see oauth_api.py) ------------------------
    app.include_router(create_oauth_router())

    # ---- OTLP observability router (v0.4 — see otlp_api.py) ---------------
    app.include_router(create_otlp_router())

    # ---- Transactions router (v0.5 — see transactions_api.py) -------------
    app.include_router(create_transactions_router())

    # ---- v0.5 load-shed admin stats ----------------------------------------

    @app.get(
        "/v1/admin/load-shed/stats",
        tags=["admin"],
    )
    async def load_shed_stats(request: Request) -> dict:
        """Return current load-shed counters.

        Permissive in dev (no inbound auth, no admin scope required) so
        operators can introspect the shedder during a benchmark run
        without rolling a token. In strict-auth deployments callers need
        ``tenant:*:admin`` or ``*``.
        """

        gw_settings: Settings = request.app.state.settings
        permitted = (
            gw_settings.auth_mode == "permissive"
            and not gw_settings.inbound_auth_required
        )
        if not permitted:
            scopes = list(getattr(request.state, "auth_scopes", []) or [])
            if "*" in scopes or "tenant:*:admin" in scopes:
                permitted = True
        if not permitted:
            raise Unauthorized(
                "load-shed stats require tenant:*:admin or * scope",
                details={"required_scope": "tenant:*:admin"},
            )
        return request.app.state.load_shedder.stats

    # ---- v0.6 revocation cache admin --------------------------------------

    @app.get(
        "/v1/admin/revocations/cache/stats",
        tags=["admin"],
    )
    async def revocation_cache_stats(request: Request) -> dict:
        """Return revocation-cache counters.

        Same permissioning rules as ``/v1/admin/load-shed/stats``: dev-mode
        deployments (permissive, no inbound auth) are open; strict-auth
        deployments need ``tenant:*:admin`` or ``*``.
        """

        gw_settings: Settings = request.app.state.settings
        permitted = (
            gw_settings.auth_mode == "permissive"
            and not gw_settings.inbound_auth_required
        )
        if not permitted:
            scopes = list(getattr(request.state, "auth_scopes", []) or [])
            if "*" in scopes or "tenant:*:admin" in scopes:
                permitted = True
        if not permitted:
            raise Unauthorized(
                "revocation cache stats require tenant:*:admin or * scope",
                details={"required_scope": "tenant:*:admin"},
            )
        rev_cache: RevocationCache = request.app.state.revocation_cache
        return rev_cache.stats

    # ---- v0.5 schema migrations admin --------------------------------------

    @app.get(
        "/v1/admin/migrations",
        tags=["admin"],
    )
    async def list_migrations(request: Request) -> dict:
        _require_admin(request)
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
        status_code=201,
        tags=["admin"],
    )
    async def apply_migrations(request: Request) -> JSONResponse:
        _require_admin(request)
        runner: MigrationRunner = request.app.state.migration_runner
        try:
            applied_migs = await runner.apply_pending(blocking_lock=False)
        except MigrationLockError as exc:
            return JSONResponse(
                status_code=409,
                content=_error_payload(
                    "MIGRATION_LOCKED", str(exc), {}
                ),
            )
        return JSONResponse(
            status_code=201,
            content={
                "applied": [_serialize_applied(m) for m in applied_migs],
            },
        )

    @app.post(
        "/v1/admin/migrations/rollback",
        response_model=RollbackResult,
        tags=["admin"],
    )
    async def rollback_migrations(
        body: RollbackBody,
        request: Request,
    ) -> RollbackResult | JSONResponse:
        """Roll back applied migrations down to (and including) ``body.to``.

        Returns 200 with the :class:`RollbackResult` payload (even when one
        of the rollback files errored mid-way — ``failed`` and
        ``error_message`` carry the partial-state info). Lock contention
        returns 409 with ``MIGRATION_LOCKED``. Missing rollback files
        bubble up as ``MIGRATION_ROLLBACK_MISSING`` (400).
        """

        _require_admin(request)
        runner: MigrationRunner = request.app.state.migration_runner
        try:
            outcome = await runner.rollback_to(
                body.to,
                dry_run=body.dry_run,
                blocking_lock=False,
            )
        except RunnerRollbackMissing as exc:
            return JSONResponse(
                status_code=400,
                content=_error_payload(
                    "MIGRATION_ROLLBACK_MISSING",
                    str(exc),
                    {"missing_ids": exc.missing_ids},
                ),
            )
        except MigrationLockError as exc:
            return JSONResponse(
                status_code=409,
                content=_error_payload(
                    "MIGRATION_LOCKED", str(exc), {}
                ),
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

    return app


# --- Helpers for /v1/admin/migrations -------------------------------------
# Plain dict serializers avoid pydantic models that mirror the runner's
# dataclasses 1:1.


def _serialize_applied(mig) -> dict:  # noqa: ANN001
    return {
        "id": mig.id,
        "checksum": mig.checksum,
        "applied_at": mig.applied_at.isoformat(),
        "duration_ms": mig.duration_ms,
        "rollback_available": getattr(mig, "rollback_available", False),
        "rollback_checksum": getattr(mig, "rollback_checksum", None),
    }


def _serialize_pending(mig) -> dict:  # noqa: ANN001
    return {
        "id": mig.id,
        "checksum": mig.checksum,
        "rollback_available": getattr(mig, "has_rollback", False),
    }


def _require_admin(request: Request) -> None:
    """Permissive deployments accept any caller; strict modes need scope."""

    settings: Settings = request.app.state.settings
    if (
        settings.auth_mode == "permissive"
        and not settings.inbound_auth_required
    ):
        return
    scopes = list(getattr(request.state, "auth_scopes", []) or [])
    if "*" in scopes or "tenant:*:admin" in scopes:
        return
    raise Unauthorized(
        "admin migrations require tenant:*:admin or * scope",
        details={"required_scope": "tenant:*:admin"},
    )


# Default module-level app for `python -m plinth_gateway` / uvicorn factories
app = create_app()
