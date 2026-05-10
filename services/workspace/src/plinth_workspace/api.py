# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app + routes for the workspace service.

Mirrors ``CONTRACTS.md → Workspace API`` 1:1.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Optional  # noqa: UP035

import structlog
from fastapi import (
    Depends,
    FastAPI,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from starlette.responses import Response as StarletteResponse

from . import __service__, __version__
from .auth import extract_auth_context_async
from .channels import ChannelStore
from .db import init_db
from .exceptions import (
    InvalidArguments,
    MigrationRollbackMissing,
    Unauthorized,
    install_exception_handlers,
)
from .gc import GCEngine, RetentionStore
from .leases import LeaseStore, lease_reaper_loop
from .coordination import CoordinationBackend, make_coordination_backend
from .load_shed import LoadShedder, load_shed_middleware
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
from .regions import RegionsResponse, RegionStatusProbe
from .replication import ReplicationLog
from .quotas import QuotaCache, QuotaEnforcer
from .resource_locks import ResourceLockStore
from .revocation_cache import RevocationCache
from .models import (
    Branch,
    BranchCreate,
    BranchList,
    Channel,
    ChannelList,
    ChannelMessage,
    ChannelMessages,
    ChannelSchema,
    ChannelSchemaSetBody,
    ChannelSendBody,
    DiffResult,
    DLQEntry,
    DLQEntryList,
    DLQReplayResult,
    FileEntry,
    FileList,
    GCResult,
    GCResultList,
    KVEntry,
    KVHistory,
    KVList,
    KVWrite,
    Lease,
    LeaseAcquireBody,
    LeaseHeartbeatBody,
    LeaseList,
    LeaseReleaseBody,
    Lock,
    LockAcquireBody,
    LockHeartbeatBody,
    LockList,
    LockReleaseBody,
    MergeResult,
    PurgeDLQResult,
    ReplayBatchBody,
    ReplayBatchResult,
    ResumeInfo,
    RetentionPolicy,
    RetentionPolicyUpdate,
    RollbackBody,
    RollbackResult,
    RolledBackMigrationModel,
    SchemaCheckBody,
    SchemaCheckResult,
    Snapshot,
    SnapshotCreate,
    SnapshotList,
    Tenant,
    TenantList,
    Worker,
    WorkerList,
    WorkerRegistration,
    Workflow,
    WorkflowCreate,
    WorkflowList,
    WorkflowStep,
    WorkflowStepCreate,
    WorkflowStepList,
    WorkflowStepUpdate,
    Workspace,
    WorkspaceCreate,
    WorkspaceList,
)
from .compliance import WorkspaceComplianceStore
from .settings import Settings, get_settings
from .snapshots import SnapshotStore
from .storage import WorkspaceStore
from .workflows import WorkflowStore, _row_to_step

# ---------------------------------------------------------------------------
# App factory


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
        settings.blobs_dir.mkdir(parents=True, exist_ok=True)
        await init_db(settings.db_path)

        # v1.1 — pluggable coordination backend (see ``coordination.py``).
        # Constructed eagerly so any code path that wants to share
        # state across replicas (lease coordination, revocation cache,
        # etc.) has a backend to call. Default ``MemoryBackend`` keeps
        # behaviour identical to v1.0; flip to ``redis`` via
        # ``PLINTH_COORDINATION_BACKEND=redis``.
        coordination: CoordinationBackend = make_coordination_backend(settings)
        app.state.coordination = coordination

        # v1.0 — replication log table. Idempotent (CREATE IF NOT EXISTS)
        # so standalone deployments pay zero cost beyond a single empty
        # table on disk; the table only fills up when ``replication_mode``
        # flips to ``"primary"`` and the routes write to it.
        await app.state.replication_log.init()

        # v0.5 — schema migrations. Apply pending migrations after init_db
        # (which is idempotent CREATE-IF-NOT-EXISTS bootstrap) so existing
        # legacy databases get marked-as-applied without re-running SQL.
        # When ``auto_migrate=False`` we still log the pending list so
        # operators see it in the boot logs. ``database_url`` triggers the
        # Postgres advisory-lock path; left empty for the SQLite default.
        runner = MigrationRunner(
            settings.db_path,
            default_migrations_dir(__file__),
            database_url=settings.effective_database_url,
            service_name="workspace",
        )
        app.state.migration_runner = runner
        try:
            if settings.auto_migrate:
                applied_migs = await runner.apply_pending(blocking_lock=True)
                if applied_migs:
                    log.info(
                        "workspace.migrations.applied",
                        count=len(applied_migs),
                        ids=[a.id for a in applied_migs],
                    )
            else:
                pending_migs = await runner.list_pending()
                if pending_migs:
                    log.warning(
                        "workspace.migrations.pending",
                        count=len(pending_migs),
                        ids=[m.id for m in pending_migs],
                        hint=(
                            "auto_migrate is disabled. Run "
                            "`python -m plinth_workspace migrate` to apply."
                        ),
                    )
        except MigrationLockError as exc:
            log.warning("workspace.migrations.locked", error=str(exc))

        # v0.5 — lease reaper. Runs only inside the workspace process so
        # we don't multi-tenant the sweeper across deployments. The
        # reaper is a no-op when no workers are running.
        reaper_stop: asyncio.Event | None = None
        reaper_task: asyncio.Task | None = None
        if settings.lease_reaper_enabled:
            reaper_stop = asyncio.Event()
            reaper_task = asyncio.create_task(
                lease_reaper_loop(
                    app.state.leases,
                    interval_seconds=settings.lease_reaper_interval_seconds,
                    inactive_timeout_seconds=settings.worker_inactive_timeout_seconds,
                    stop_event=reaper_stop,
                    resource_locks=app.state.resource_locks,
                ),
                name="plinth-workspace-lease-reaper",
            )
            app.state.lease_reaper_stop = reaper_stop
            app.state.lease_reaper_task = reaper_task

        # v0.6 — federated revocation cache. Populated from Identity once
        # at startup (so the first request gets a warm cache) and refreshed
        # every ``revocation_poll_interval_seconds`` thereafter. Disabled
        # by default (empty URL) to keep single-node setups + v0.5 demos
        # working without configuration changes.
        rev_cache: RevocationCache = app.state.revocation_cache
        if (
            settings.revocation_poll_url
            and settings.revocation_poll_enabled
        ):
            try:
                await rev_cache.start()
                log.info(
                    "workspace.revocation_cache.started",
                    identity_url=rev_cache.identity_url,
                    poll_interval=rev_cache.poll_interval,
                    initial_size=rev_cache.stats["size"],
                )
            except Exception as exc:  # noqa: BLE001 - never break startup
                log.warning(
                    "workspace.revocation_cache.start_failed",
                    error=str(exc),
                )

        # Loud warning when we're running in legacy auth mode: every demo
        # in v0.1/v0.2 relies on this, so we don't error — but operators
        # should see this in the logs immediately.
        if settings.auth_mode == "permissive" and not settings.identity_jwt_secret:
            log.warning(
                "workspace.auth.disabled",
                hint=(
                    "AUTH DISABLED: every request lands in tenant 'default'. "
                    "Set PLINTH_AUTH_MODE=verify_local + "
                    "PLINTH_IDENTITY_JWT_SECRET to enforce JWTs."
                ),
            )
        elif (
            settings.auth_mode in ("verify_local", "verify_remote")
            and settings.auth_mode == "verify_local"
            and not settings.identity_jwt_secret
        ):
            # An RS256 deployment doesn't need a shared secret — the
            # verifier resolves keys via JWKS. We only fail closed when
            # there's neither a secret nor an identity URL (the JWKS
            # endpoint).
            if not settings.identity_url:
                raise RuntimeError(
                    "PLINTH_AUTH_MODE=verify_local requires either "
                    "PLINTH_IDENTITY_JWT_SECRET (HS256) or "
                    "PLINTH_IDENTITY_URL (RS256 via JWKS)",
                )

        log.info(
            "workspace.startup",
            data_dir=str(settings.data_dir),
            db_path=str(settings.db_path),
            port=settings.workspace_port,
            auth_mode=settings.auth_mode,
        )
        yield

        # Stop the lease reaper before tearing the rest of the app down.
        if reaper_stop is not None:
            reaper_stop.set()
        if reaper_task is not None:
            try:
                await asyncio.wait_for(reaper_task, timeout=5.0)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                reaper_task.cancel()
                try:
                    await reaper_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        # Stop the revocation polling loop. Safe to call even if start()
        # never ran (e.g. revocation_poll_url was empty).
        try:
            await app.state.revocation_cache.stop()
        except Exception as exc:  # noqa: BLE001 - never break shutdown
            log.warning(
                "workspace.revocation_cache.stop_failed",
                error=str(exc),
            )

        try:
            await coordination.aclose()
        except Exception as exc:  # noqa: BLE001 - never break shutdown
            log.warning(
                "workspace.coordination.close_failed", error=str(exc)
            )

        log.info("workspace.shutdown")

    app = FastAPI(
        title="plinth-workspace",
        version=__version__,
        description="Plinth workspace service — versioned KV + file storage.",
        lifespan=lifespan,
    )

    # Stash dependencies on the app so handlers can pull them via Depends.
    app.state.settings = settings
    app.state.store = WorkspaceStore(settings.db_path, settings.blobs_dir)
    app.state.snapshots = SnapshotStore(app.state.store)
    app.state.channels = ChannelStore(settings.db_path)
    app.state.workflows = WorkflowStore(settings.db_path)
    app.state.retention = RetentionStore(settings.db_path)
    app.state.gc_engine = GCEngine(settings.db_path, settings.blobs_dir)
    app.state.leases = LeaseStore(settings.db_path)
    # v0.6 — generic resource locks. Independent of the workflow-step lease
    # primitive; the same reaper task sweeps both tables (see leases.py).
    app.state.resource_locks = ResourceLockStore(settings.db_path)
    # v0.5 — migration runner. Constructed eagerly so the admin endpoints
    # work even in test setups that bypass the lifespan handler. Forwards
    # ``database_url`` + ``service_name`` so v0.6 Postgres advisory locks
    # take effect transparently (no-op for SQLite deployments).
    app.state.migration_runner = MigrationRunner(
        settings.db_path,
        default_migrations_dir(__file__),
        database_url=settings.effective_database_url,
        service_name="workspace",
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

    # v1.0 — Prometheus metrics. Constructed eagerly so the middleware can
    # reach it on the very first request. Pre-declares every workspace-
    # specific metric so ``GET /metrics`` always returns the canonical
    # series even before any request has flowed through (Prometheus needs
    # the ``# TYPE`` lines to start scoring rate() correctly).
    metrics = MetricsRegistry(
        service_name=__service__,
        version=__version__,
    )
    metrics.declare_counter(
        "plinth_kv_writes_total",
        "Total KV writes (puts + deletes).",
    )
    metrics.declare_counter(
        "plinth_files_writes_total",
        "Total file writes (puts + deletes).",
    )
    metrics.declare_gauge(
        "plinth_workspaces_total",
        "Number of workspaces (gauge, by tenant).",
    )
    metrics.declare_gauge(
        "plinth_storage_bytes",
        "Total bytes stored across blobs (gauge, by tenant).",
    )
    metrics.declare_gauge(
        "plinth_workflows_active",
        "Active workflows (gauge, by tenant).",
    )
    metrics.declare_gauge(
        "plinth_workflow_steps_total",
        "Workflow steps by state (gauge).",
    )
    metrics.declare_gauge(
        "plinth_workers_active",
        "Active workers (gauge).",
    )
    metrics.declare_counter(
        "plinth_load_shed_total",
        "Requests rejected by the load shedder.",
    )
    app.state.metrics = metrics

    # v0.6 — federated revocation cache. Constructed eagerly so the auth
    # middleware + admin/stats endpoint can rely on its presence, even
    # when polling is disabled (start() is gated by settings inside the
    # lifespan handler).
    app.state.revocation_cache = RevocationCache(
        identity_url=settings.revocation_poll_url,
        poll_interval=settings.revocation_poll_interval_seconds,
    )

    # v1.0 — per-tenant quota enforcement. Always constructed so the
    # admin/stats and routing code can rely on its presence; ``enabled``
    # comes from ``settings.quotas_enabled`` (default False) so v0.6 demos
    # see a no-op.
    app.state.quota_enforcer = QuotaEnforcer(
        QuotaCache(
            identity_url=settings.identity_url,
            ttl_seconds=settings.quotas_cache_ttl_seconds,
            timeout_seconds=settings.quotas_fetch_timeout_seconds,
        ),
        db_path=settings.db_path,
        enabled=settings.quotas_enabled,
    )

    # v1.0 — multi-region scaffolding. The probe runs only when an operator
    # actually configures peers; ``standalone`` deployments incur no cost.
    # The replication log table is initialised below in ``init_db`` (it's
    # additive; reading is cheap) but only written to when ``replication_mode``
    # flips to ``"primary"``.
    app.state.region_status_probe = RegionStatusProbe(
        cache_ttl_seconds=settings.regions_status_cache_ttl_seconds,
        probe_timeout_seconds=settings.regions_status_probe_timeout_seconds,
    )
    app.state.replication_log = ReplicationLog(
        settings.db_path,
        region_id=settings.region_id,
    )

    install_exception_handlers(app)
    # Order matters: middleware registered LAST runs FIRST on inbound
    # requests. We want load-shedding to be the outermost gate so a
    # rejected request never touches auth or any downstream state.
    # The metrics middleware sits between auth and load-shed so it sees
    # genuine traffic (not 503s) and the histograms stay clean.
    app.middleware("http")(metrics_middleware_factory(metrics))
    app.middleware("http")(_request_context_middleware)
    app.middleware("http")(_replica_redirect_middleware)
    app.middleware("http")(load_shed_middleware)

    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Middleware


async def _request_context_middleware(request: Request, call_next):
    """Attach a request_id + auth context to every request.

    The middleware always sets ``request.state.tenant_id`` and
    ``request.state.agent_id``. In ``permissive`` mode (the default) those
    default to ``"default"`` and ``None`` so handlers can rely on them
    unconditionally.
    """

    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        service=__service__,
        request_id=request_id,
        path=request.url.path,
        method=request.method,
    )
    log = get_logger()

    settings: Settings = request.app.state.settings
    request.state.tenant_id = "default"
    request.state.agent_id = None
    request.state.auth_scopes = []

    if request.url.path != "/healthz":
        auth_header = request.headers.get("authorization", "")

        # 1) Resolve the auth context (raises Unauthorized for verify_* modes
        #    when the token is bad). We catch it here so we can return the
        #    standard error envelope without leaning on the global handler
        #    (which only fires for raises *inside* a route).
        try:
            ctx = await extract_auth_context_async(auth_header, settings)
        except Unauthorized as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                },
            )

        # v0.6 — federated revocation. After a successful JWT decode, the
        # caller's JTI may already be on the in-memory blocklist (populated
        # from Identity's ``GET /v1/revocations``). Reject with a stable
        # TOKEN_REVOKED envelope so SDK clients can react. The cache is
        # only consulted in non-permissive auth modes — permissive mode
        # has no JTI to check most of the time anyway.
        if (
            settings.auth_mode in ("verify_local", "verify_remote")
            and ctx.authenticated
            and ctx.jti is not None
        ):
            rev_cache = getattr(request.app.state, "revocation_cache", None)
            if rev_cache is not None and rev_cache.is_revoked(ctx.jti):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "TOKEN_REVOKED",
                            "message": "token has been revoked",
                            "details": {"jti": ctx.jti},
                        }
                    },
                )

        request.state.tenant_id = ctx.tenant_id
        request.state.agent_id = ctx.agent_id
        request.state.auth_scopes = ctx.scopes or []
        structlog.contextvars.bind_contextvars(tenant_id=ctx.tenant_id)
        if ctx.agent_id:
            structlog.contextvars.bind_contextvars(agent_id=ctx.agent_id)

        # 2) Permissive mode keeps the legacy ``auth_required`` knob alive:
        #    callers can still demand a non-empty bearer token without
        #    flipping to JWT verification.
        if settings.auth_mode == "permissive":
            token = ""
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1].strip()
            if not token:
                if settings.auth_required:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": {
                                "code": Unauthorized.code,
                                "message": Unauthorized.message,
                                "details": {},
                            }
                        },
                    )
                log.warning("workspace.auth.missing_token")

    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# v1.0 — multi-region read-replica middleware.
# When the local instance is a ``replica``, every mutating verb returns
# 421 (Misdirected Request) with both ``X-Plinth-Primary-Region`` and
# ``X-Plinth-Primary-URL`` headers populated so the SDK can transparently
# retry the request against the primary. A ``Location`` header is also
# emitted for plain-HTTP clients (curl, etc.) that follow standard
# redirects. Standalone + primary deployments bypass this entirely.
# Primaries with ``replication_mode='primary'`` route through the same
# middleware so successful mutations append to the replication log on
# the way out — replicas pull and replay from there.
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
# These paths are always allowed even on a replica — operators need
# them to introspect the deployment and orchestrate replication pulls.
_REPLICA_ALLOWLIST = (
    "/healthz",
    "/v1/regions",
    "/v1/admin/replication/",
    "/metrics",
)


def _classify_mutation(method: str, path: str) -> tuple[str, str | None] | None:
    """Map an HTTP method+path to a replication-log ``kind`` + workspace_id.

    Returns ``None`` for paths that don't represent meaningful mutations
    (admin endpoints, OAuth callbacks, etc.) — those don't get logged.
    """

    if method not in _MUTATING_METHODS:
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0] != "v1":
        return None
    if parts[1] != "workspaces":
        # Non-workspace mutations: workflows top-level, tenants, etc. We
        # log them with no workspace context so a replica can replay.
        return f"http.{method.lower()}.{parts[1]}", None
    if len(parts) < 3:
        # POST /v1/workspaces — workspace creation.
        return f"workspace.{method.lower()}", None
    ws_id = parts[2]
    if len(parts) == 3:
        return f"workspace.{method.lower()}", ws_id
    suffix = ".".join(parts[3:])
    return f"workspace.{method.lower()}.{suffix}", ws_id


async def _replica_redirect_middleware(request: Request, call_next):
    """Short-circuit mutating writes when ``replication_mode == 'replica'``."""

    settings: Settings = request.app.state.settings

    if settings.replication_mode == "replica" and request.method in _MUTATING_METHODS:
        path = request.url.path
        if not any(path.startswith(prefix) for prefix in _REPLICA_ALLOWLIST):
            return _replica_redirect_response(settings, path)

    response = await call_next(request)

    # On the primary, append a log entry for every successful mutation so
    # replicas can pull + replay later. Failures are not logged; a partial
    # write doesn't actually mutate state on the primary.
    if (
        settings.replication_mode == "primary"
        and request.method in _MUTATING_METHODS
        and 200 <= response.status_code < 400
    ):
        path = request.url.path
        if not any(path.startswith(prefix) for prefix in _REPLICA_ALLOWLIST):
            classified = _classify_mutation(request.method, path)
            if classified is not None:
                kind, ws_id = classified
                try:
                    log_store: ReplicationLog = request.app.state.replication_log
                    await log_store.append(
                        kind,
                        {
                            "method": request.method,
                            "path": path,
                            "status": response.status_code,
                        },
                        workspace_id=ws_id,
                    )
                except Exception:  # pragma: no cover - never break a write
                    get_logger().warning(
                        "workspace.replication.log_append_failed",
                        path=path,
                        method=request.method,
                    )
    return response


def _replica_redirect_response(settings: Settings, path: str) -> JSONResponse:
    """Return the 421 ``REPLICA_READ_ONLY`` envelope for a mutating call.

    The 421 (Misdirected Request) status is RFC 7540 §9.1.2: "the request
    was directed at a server that is not able to produce a response".
    That fits the read-replica case exactly — the request is
    syntactically fine but addressed to the wrong host.
    """

    # The primary region id is the first configured peer (replicas
    # declare their primary as a peer). Falls back to ``region_id`` so
    # an operator that hasn't fully wired peers still gets a sensible
    # header instead of an empty string.
    primary_id = settings.region_peers[0] if settings.region_peers else settings.region_id
    primary_url = settings.region_primary_url or settings.region_peer_urls.get(primary_id, "")
    headers: dict[str, str] = {"X-Plinth-Primary-Region": primary_id}
    if primary_url:
        # ``X-Plinth-Primary-URL`` is the SDK-readable redirect target.
        # ``Location`` is emitted alongside for plain-HTTP / curl users
        # who follow standard redirects.
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


def _get_store(request: Request) -> WorkspaceStore:
    return request.app.state.store


def _get_snapshots(request: Request) -> SnapshotStore:
    return request.app.state.snapshots


def _get_channels(request: Request) -> ChannelStore:
    return request.app.state.channels


def _get_workflows(request: Request) -> WorkflowStore:
    return request.app.state.workflows


def _get_retention(request: Request) -> RetentionStore:
    return request.app.state.retention


def _get_gc_engine(request: Request) -> GCEngine:
    return request.app.state.gc_engine


def _get_leases(request: Request) -> LeaseStore:
    return request.app.state.leases


def _get_resource_locks(request: Request) -> ResourceLockStore:
    return request.app.state.resource_locks


def _get_quota_enforcer(request: Request) -> QuotaEnforcer:
    return request.app.state.quota_enforcer


StoreDep = Annotated[WorkspaceStore, Depends(_get_store)]
SnapshotsDep = Annotated[SnapshotStore, Depends(_get_snapshots)]
ChannelsDep = Annotated[ChannelStore, Depends(_get_channels)]
WorkflowsDep = Annotated[WorkflowStore, Depends(_get_workflows)]
RetentionDep = Annotated[RetentionStore, Depends(_get_retention)]
GCEngineDep = Annotated[GCEngine, Depends(_get_gc_engine)]
LeasesDep = Annotated[LeaseStore, Depends(_get_leases)]
ResourceLocksDep = Annotated[ResourceLockStore, Depends(_get_resource_locks)]
QuotaEnforcerDep = Annotated[QuotaEnforcer, Depends(_get_quota_enforcer)]
# These two type aliases are evaluated at runtime; on 3.11+ ``str | None``
# resolves to a UnionType, but on 3.9 we need ``Optional`` for the install
# path that runs `pip install -e .` to even import the module.
BranchQuery = Annotated[Optional[str], Query(alias="branch")]  # noqa: UP007, UP045
VersionQuery = Annotated[Optional[int], Query(ge=1)]  # noqa: UP007, UP045


# ---------------------------------------------------------------------------
# Routes


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__, "service": __service__}

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics_endpoint(request: Request):
        """Prometheus exposition endpoint.

        Refreshes a small set of gauges (workspace count, storage bytes,
        active workflows, workers, workflow-step states, load-shed
        counters) on each scrape so operators get fresh values without a
        separate sweeper task. The refresh is best-effort: any failure is
        swallowed so the scrape always succeeds.
        """

        registry: MetricsRegistry = request.app.state.metrics
        try:
            await _refresh_workspace_gauges(request.app, registry)
        except Exception:  # noqa: BLE001 — metrics must never crash a scrape
            pass
        return metrics_response(registry)

    # ------------------------------------------------------------------ workspaces

    @app.post(
        "/v1/workspaces",
        response_model=Workspace,
        status_code=status.HTTP_201_CREATED,
        tags=["workspaces"],
    )
    async def create_workspace(
        body: WorkspaceCreate,
        store: StoreDep,
        quotas: QuotaEnforcerDep,
        request: Request,
    ) -> Workspace:
        tenant_id = getattr(request.state, "tenant_id", "default")
        # v1.0 — per-tenant quota enforcement (no-op when disabled).
        await quotas.check_workspace_create(tenant_id)
        ws = await store.create_workspace(body.name, body.metadata, tenant_id=tenant_id)
        get_logger().info(
            "workspace.created",
            workspace_id=ws.id,
            name=ws.name,
            tenant_id=tenant_id,
        )
        return ws

    @app.get("/v1/workspaces", response_model=WorkspaceList, tags=["workspaces"])
    async def list_workspaces(store: StoreDep, request: Request) -> WorkspaceList:
        tenant_id = getattr(request.state, "tenant_id", "default")
        return WorkspaceList(
            workspaces=await store.list_workspaces(tenant_id=tenant_id)
        )

    @app.get(
        "/v1/tenants",
        response_model=TenantList,
        tags=["workspaces"],
    )
    async def list_tenants(store: StoreDep) -> TenantList:
        rows = await store.list_tenants()
        return TenantList(tenants=[Tenant(**row) for row in rows])

    @app.get(
        "/v1/workspaces/{ws_id}",
        response_model=Workspace,
        tags=["workspaces"],
    )
    async def get_workspace(
        ws_id: str,
        store: StoreDep,
        request: Request,
    ) -> Workspace:
        tenant_id = getattr(request.state, "tenant_id", "default")
        return await store.get_workspace(ws_id, tenant_id=tenant_id)

    @app.delete(
        "/v1/workspaces/{ws_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["workspaces"],
    )
    async def delete_workspace(
        ws_id: str,
        store: StoreDep,
        request: Request,
    ) -> Response:
        tenant_id = getattr(request.state, "tenant_id", "default")
        # Visibility check first — same 404 the GET path returns.
        await store.get_workspace(ws_id, tenant_id=tenant_id)
        await store.delete_workspace(ws_id)
        get_logger().info(
            "workspace.deleted",
            workspace_id=ws_id,
            tenant_id=tenant_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ KV

    @app.put(
        "/v1/workspaces/{ws_id}/kv/{key:path}",
        response_model=KVEntry,
        tags=["kv"],
    )
    async def kv_put(
        ws_id: str,
        key: str,
        body: KVWrite,
        store: StoreDep,
        request: Request,
        branch: BranchQuery = None,
    ) -> KVEntry:
        if not key:
            raise InvalidArguments("key must be non-empty")
        result = await store.kv_put(ws_id, key, body.value, branch_id=branch)
        tenant_id = getattr(request.state, "tenant_id", "default")
        request.app.state.metrics.counter(
            "plinth_kv_writes_total", {"tenant_id": tenant_id}
        ).inc(1)
        return result

    @app.get(
        "/v1/workspaces/{ws_id}/kv/{key:path}/history",
        response_model=KVHistory,
        tags=["kv"],
    )
    async def kv_history(
        ws_id: str,
        key: str,
        store: StoreDep,
        branch: BranchQuery = None,
    ) -> KVHistory:
        return KVHistory(versions=await store.kv_history(ws_id, key, branch_id=branch))

    @app.get(
        "/v1/workspaces/{ws_id}/kv",
        response_model=KVList,
        tags=["kv"],
    )
    async def kv_list(
        ws_id: str,
        store: StoreDep,
        branch: BranchQuery = None,
    ) -> KVList:
        return KVList(entries=await store.kv_list(ws_id, branch_id=branch))

    @app.get(
        "/v1/workspaces/{ws_id}/kv/{key:path}",
        response_model=KVEntry,
        tags=["kv"],
    )
    async def kv_get(
        ws_id: str,
        key: str,
        store: StoreDep,
        version: VersionQuery = None,
        branch: BranchQuery = None,
    ) -> KVEntry:
        return await store.kv_get(ws_id, key, version=version, branch_id=branch)

    @app.delete(
        "/v1/workspaces/{ws_id}/kv/{key:path}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["kv"],
    )
    async def kv_delete(
        ws_id: str,
        key: str,
        store: StoreDep,
        request: Request,
        branch: BranchQuery = None,
    ) -> Response:
        await store.kv_delete(ws_id, key, branch_id=branch)
        tenant_id = getattr(request.state, "tenant_id", "default")
        request.app.state.metrics.counter(
            "plinth_kv_writes_total", {"tenant_id": tenant_id}
        ).inc(1)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ files

    @app.put(
        "/v1/workspaces/{ws_id}/files/{path:path}",
        response_model=FileEntry,
        tags=["files"],
    )
    async def file_put(
        ws_id: str,
        path: str,
        request: Request,
        store: StoreDep,
        quotas: QuotaEnforcerDep,
        branch: BranchQuery = None,
    ) -> FileEntry:
        if not path:
            raise InvalidArguments("file path must be non-empty")
        data = await request.body()
        # v1.0 — per-tenant storage quota. Run BEFORE any disk write so a
        # tenant can't sneak past the cap by partial write + abort.
        tenant_id = getattr(request.state, "tenant_id", "default")
        await quotas.check_file_storage(tenant_id, len(data))
        ct_header = request.headers.get("content-type")
        # Treat the FastAPI default of "application/json" as "no opinion"
        # only if the client probably meant to send raw bytes.
        result = await store.file_put(
            ws_id,
            path,
            data,
            content_type=ct_header,
            branch_id=branch,
        )
        request.app.state.metrics.counter(
            "plinth_files_writes_total", {"tenant_id": tenant_id}
        ).inc(1)
        return result

    @app.get(
        "/v1/workspaces/{ws_id}/files",
        response_model=FileList,
        tags=["files"],
    )
    async def file_list(
        ws_id: str,
        store: StoreDep,
        branch: BranchQuery = None,
    ) -> FileList:
        return FileList(files=await store.file_list(ws_id, branch_id=branch))

    @app.get(
        "/v1/workspaces/{ws_id}/files/{path:path}/meta",
        response_model=FileEntry,
        tags=["files"],
    )
    async def file_meta(
        ws_id: str,
        path: str,
        store: StoreDep,
        version: VersionQuery = None,
        branch: BranchQuery = None,
    ) -> FileEntry:
        return await store.file_get_meta(
            ws_id, path, version=version, branch_id=branch
        )

    @app.get(
        "/v1/workspaces/{ws_id}/files/{path:path}",
        tags=["files"],
        response_class=StarletteResponse,
    )
    async def file_read(
        ws_id: str,
        path: str,
        store: StoreDep,
        version: VersionQuery = None,
        branch: BranchQuery = None,
    ) -> StarletteResponse:
        meta, data = await store.file_read(
            ws_id, path, version=version, branch_id=branch
        )
        return StarletteResponse(
            content=data,
            media_type=meta.content_type,
            headers={
                "x-plinth-version": str(meta.version),
                "x-plinth-sha256": meta.sha256,
                "x-plinth-size": str(meta.size),
            },
        )

    @app.delete(
        "/v1/workspaces/{ws_id}/files/{path:path}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["files"],
    )
    async def file_delete(
        ws_id: str,
        path: str,
        store: StoreDep,
        request: Request,
        branch: BranchQuery = None,
    ) -> Response:
        await store.file_delete(ws_id, path, branch_id=branch)
        tenant_id = getattr(request.state, "tenant_id", "default")
        request.app.state.metrics.counter(
            "plinth_files_writes_total", {"tenant_id": tenant_id}
        ).inc(1)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ snapshots

    @app.post(
        "/v1/workspaces/{ws_id}/snapshots",
        response_model=Snapshot,
        status_code=status.HTTP_201_CREATED,
        tags=["snapshots"],
    )
    async def create_snapshot(
        ws_id: str,
        body: SnapshotCreate,
        snapshots: SnapshotsDep,
        branch: BranchQuery = None,
    ) -> Snapshot:
        snap = await snapshots.create_snapshot(
            ws_id, body.name, message=body.message, branch_id=branch
        )
        get_logger().info(
            "workspace.snapshot.created",
            workspace_id=ws_id,
            snapshot_id=snap.id,
            branch_id=branch,
        )
        return snap

    @app.get(
        "/v1/workspaces/{ws_id}/snapshots",
        response_model=SnapshotList,
        tags=["snapshots"],
    )
    async def list_snapshots(ws_id: str, snapshots: SnapshotsDep) -> SnapshotList:
        return SnapshotList(snapshots=await snapshots.list_snapshots(ws_id))

    @app.get(
        "/v1/workspaces/{ws_id}/snapshots/{snap_id}/diff",
        response_model=DiffResult,
        tags=["snapshots"],
    )
    async def diff_snapshot(
        ws_id: str,
        snap_id: str,
        snapshots: SnapshotsDep,
        against: Annotated[str, Query(min_length=1)],
    ) -> DiffResult:
        return await snapshots.diff_snapshots(ws_id, snap_id, against)

    @app.get(
        "/v1/workspaces/{ws_id}/snapshots/{snap_id}",
        response_model=Snapshot,
        tags=["snapshots"],
    )
    async def get_snapshot(
        ws_id: str,
        snap_id: str,
        snapshots: SnapshotsDep,
    ) -> Snapshot:
        return await snapshots.get_snapshot(ws_id, snap_id)

    # ------------------------------------------------------------------ branches

    @app.post(
        "/v1/workspaces/{ws_id}/branches",
        response_model=Branch,
        status_code=status.HTTP_201_CREATED,
        tags=["branches"],
    )
    async def create_branch(
        ws_id: str,
        body: BranchCreate,
        snapshots: SnapshotsDep,
    ) -> Branch:
        br = await snapshots.create_branch(ws_id, body.name, body.from_snapshot)
        get_logger().info(
            "workspace.branch.created",
            workspace_id=ws_id,
            branch_id=br.id,
            from_snapshot_id=br.from_snapshot_id,
        )
        return br

    @app.get(
        "/v1/workspaces/{ws_id}/branches",
        response_model=BranchList,
        tags=["branches"],
    )
    async def list_branches(ws_id: str, snapshots: SnapshotsDep) -> BranchList:
        return BranchList(branches=await snapshots.list_branches(ws_id))

    @app.post(
        "/v1/workspaces/{ws_id}/branches/{branch_id}/merge",
        response_model=MergeResult,
        tags=["branches"],
    )
    async def merge_branch(
        ws_id: str,
        branch_id: str,
        snapshots: SnapshotsDep,
    ) -> MergeResult:
        result = await snapshots.merge_branch(ws_id, branch_id)
        get_logger().info(
            "workspace.branch.merged",
            workspace_id=ws_id,
            branch_id=branch_id,
            kv_merged=len(result.kv_merged),
            files_merged=len(result.files_merged),
        )
        return result

    @app.delete(
        "/v1/workspaces/{ws_id}/branches/{branch_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["branches"],
    )
    async def delete_branch(
        ws_id: str,
        branch_id: str,
        snapshots: SnapshotsDep,
    ) -> Response:
        await snapshots.delete_branch(ws_id, branch_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ channels

    @app.post(
        "/v1/workspaces/{ws_id}/channels/{name}/send",
        response_model=ChannelMessage,
        status_code=status.HTTP_201_CREATED,
        tags=["channels"],
    )
    async def channel_send(
        ws_id: str,
        name: str,
        body: ChannelSendBody,
        channels: ChannelsDep,
        quotas: QuotaEnforcerDep,
        request: Request,
    ) -> ChannelMessage:
        # v1.0 — channel auto-create quota check. Only enforces when this
        # would create a new channel; existing channels are unaffected.
        tenant_id = getattr(request.state, "tenant_id", "default")
        await quotas.check_channel_autocreate(tenant_id, ws_id, name)
        msg = await channels.send(
            ws_id,
            name,
            payload=body.payload,
            sender=body.sender,
            type_=body.type,
            correlation_id=body.correlation_id,
            headers=body.headers,
        )
        get_logger().info(
            "workspace.channel.sent",
            workspace_id=ws_id,
            channel=name,
            seq=msg.seq,
            message_id=msg.id,
        )
        return msg

    @app.get(
        "/v1/workspaces/{ws_id}/channels/{name}/receive",
        response_model=ChannelMessages,
        tags=["channels"],
    )
    async def channel_receive(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
        since: Annotated[Optional[int], Query(ge=0)] = None,  # noqa: UP007, UP045
        limit: Annotated[Optional[int], Query(ge=1, le=1000)] = None,  # noqa: UP007, UP045
        consumer: Annotated[Optional[str], Query()] = None,  # noqa: UP007, UP045
        peek: Annotated[bool, Query()] = False,
    ) -> ChannelMessages:
        msgs = await channels.receive(
            ws_id,
            name,
            since=since,
            limit=limit,
            consumer=consumer,
            peek=peek,
        )
        return ChannelMessages(messages=msgs)

    @app.delete(
        "/v1/workspaces/{ws_id}/channels/{name}/messages/{message_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["channels"],
    )
    async def channel_delete_message(
        ws_id: str,
        name: str,
        message_id: str,
        channels: ChannelsDep,
    ) -> Response:
        await channels.delete_message(ws_id, name, message_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/workspaces/{ws_id}/channels",
        response_model=ChannelList,
        tags=["channels"],
    )
    async def list_channels(
        ws_id: str,
        channels: ChannelsDep,
    ) -> ChannelList:
        return ChannelList(channels=await channels.list_channels(ws_id))

    # ----- v0.5: typed channels (schema CRUD + dead-letter queue) -----
    # Declared BEFORE the catch-all ``{name}`` routes so FastAPI matches
    # ``/schema`` and ``/deadletter`` segments to these handlers and not to
    # the get/delete-channel handlers below.

    @app.post(
        "/v1/workspaces/{ws_id}/channels/{name:path}/schema",
        response_model=ChannelSchema,
        tags=["channels"],
    )
    async def set_channel_schema(
        ws_id: str,
        name: str,
        body: ChannelSchemaSetBody,
        channels: ChannelsDep,
    ) -> ChannelSchema:
        sch = await channels.set_schema(ws_id, name, body.schema_doc)
        get_logger().info(
            "workspace.channel.schema.set",
            workspace_id=ws_id,
            channel=name,
            version=sch.version,
        )
        return sch

    @app.get(
        "/v1/workspaces/{ws_id}/channels/{name:path}/schema",
        response_model=ChannelSchema,
        tags=["channels"],
    )
    async def get_channel_schema(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
    ) -> ChannelSchema:
        sch = await channels.get_schema(ws_id, name)
        if sch is None:
            # 404 with a CHANNEL_NOT_FOUND-style envelope, but distinct
            # code so callers can tell "no schema" apart from "no channel".
            raise InvalidArguments(
                f"no schema attached to channel {name!r}",
                code="SCHEMA_NOT_FOUND",
                status_code=404,
                details={"workspace_id": ws_id, "channel": name},
            )
        return sch

    @app.delete(
        "/v1/workspaces/{ws_id}/channels/{name:path}/schema",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["channels"],
    )
    async def delete_channel_schema(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
    ) -> Response:
        # Unknown schema is a no-op 204; the spec is silent on the 404
        # case and the principle of least astonishment for delete-cleanup
        # idempotency wins.
        await channels.delete_schema(ws_id, name)
        get_logger().info(
            "workspace.channel.schema.deleted",
            workspace_id=ws_id,
            channel=name,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/workspaces/{ws_id}/channels/{name:path}/deadletter",
        response_model=ChannelMessages,
        tags=["channels"],
    )
    async def list_deadletter(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
        since: Annotated[Optional[int], Query(ge=0)] = None,  # noqa: UP007, UP045
        limit: Annotated[Optional[int], Query(ge=1, le=1000)] = None,  # noqa: UP007, UP045
    ) -> ChannelMessages:
        msgs = await channels.list_deadletters(
            ws_id, name, since=since, limit=limit
        )
        return ChannelMessages(messages=msgs)

    @app.post(
        "/v1/workspaces/{ws_id}/channels/{name:path}/deadletter/{msg_id}/replay",
        response_model=ChannelMessage,
        tags=["channels"],
    )
    async def replay_deadletter(
        ws_id: str,
        name: str,
        msg_id: str,
        channels: ChannelsDep,
    ) -> ChannelMessage:
        msg = await channels.replay_deadletter(ws_id, name, msg_id)
        get_logger().info(
            "workspace.channel.deadletter.replayed",
            workspace_id=ws_id,
            channel=name,
            original_id=msg_id,
            new_id=msg.id,
        )
        return msg

    @app.delete(
        "/v1/workspaces/{ws_id}/channels/{name:path}/deadletter/{msg_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["channels"],
    )
    async def drop_deadletter(
        ws_id: str,
        name: str,
        msg_id: str,
        channels: ChannelsDep,
    ) -> Response:
        await channels.drop_deadletter(ws_id, name, msg_id)
        get_logger().info(
            "workspace.channel.deadletter.dropped",
            workspace_id=ws_id,
            channel=name,
            message_id=msg_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ----- v0.6: schema migration helpers (additive) -----
    # ``schema/check`` is the dry-run preview before committing a new
    # schema; ``deadletter/replay-all`` and ``deadletter`` (DELETE bulk)
    # let operators clear backlogs without iterating message-by-message.

    @app.post(
        "/v1/workspaces/{ws_id}/channels/{name:path}/schema/check",
        response_model=SchemaCheckResult,
        tags=["channels"],
    )
    async def check_channel_schema(
        ws_id: str,
        name: str,
        body: SchemaCheckBody,
        channels: ChannelsDep,
    ) -> SchemaCheckResult:
        result = await channels.check_schema(
            ws_id,
            name,
            body.schema_doc,
            scope=body.scope,
            limit=body.limit,
        )
        get_logger().info(
            "workspace.channel.schema.checked",
            workspace_id=ws_id,
            channel=name,
            scope=body.scope,
            checked=result["checked"],
            invalid=result["invalid"],
        )
        return SchemaCheckResult.model_validate(result)

    @app.post(
        "/v1/workspaces/{ws_id}/channels/{name:path}/deadletter/replay-all",
        response_model=ReplayBatchResult,
        tags=["channels"],
    )
    async def replay_all_deadletter(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
        request: Request,
        # ``max`` is a Python builtin so we accept it via Query alias and
        # rebind to a less-shadowy local name. The 1..10_000 bound mirrors
        # ``BULK_HARD_LIMIT`` on the store.
        max_messages: Annotated[int | None, Query(alias="max", ge=1, le=10000)] = None,
        dry_run: Annotated[bool | None, Query()] = None,
    ) -> ReplayBatchResult:
        # The endpoint supports two equivalent shapes:
        #   * query string: ``?dry_run=true&max=50``
        #   * JSON body:    ``{"dry_run": true, "max": 50}``
        # The body form is documented as the "recommended" shape because
        # replay-all is mutating; the query form stays around so curl
        # smoke-tests don't have to construct a body. If both are present
        # the explicit body wins (treat the query as a default).
        body_dry_run: bool | None = None
        body_max: int | None = None
        # Only attempt to parse a body if the caller actually sent one —
        # FastAPI's body parser otherwise rejects ``Content-Length: 0``.
        try:
            raw = await request.body()
        except Exception:  # noqa: BLE001 — defensive
            raw = b""
        if raw:
            try:
                parsed = ReplayBatchBody.model_validate_json(raw)
                body_dry_run = parsed.dry_run
                body_max = parsed.max
            except Exception as exc:  # noqa: BLE001 — surface as InvalidArguments
                raise InvalidArguments(
                    "invalid replay-all body",
                    details={"reason": str(exc)},
                )

        # Resolve the effective parameters. Body wins; otherwise query;
        # otherwise default to (False, 100) per the v0.6 spec.
        effective_dry_run = (
            body_dry_run
            if body_dry_run is not None
            else (dry_run if dry_run is not None else False)
        )
        effective_max = (
            body_max if body_max is not None else (max_messages or 100)
        )

        result = await channels.replay_all_deadletter(
            ws_id,
            name,
            max_messages=effective_max,
            dry_run=effective_dry_run,
        )
        get_logger().info(
            "workspace.channel.deadletter.replay_all",
            workspace_id=ws_id,
            channel=name,
            attempted=result["attempted"],
            succeeded=result["succeeded"],
            failed=result["failed"],
            dry_run=effective_dry_run,
        )
        return ReplayBatchResult.model_validate(result)

    @app.delete(
        "/v1/workspaces/{ws_id}/channels/{name:path}/deadletter",
        response_model=PurgeDLQResult,
        tags=["channels"],
    )
    async def purge_deadletter(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
        older_than_seconds: Annotated[int, Query(ge=0)] = 0,
    ) -> PurgeDLQResult:
        # The spec mentioned a 204 + header alternative; we return 200 with
        # a body because typed clients are easier to write that way and
        # tests get a clean assertion target.
        purged = await channels.purge_deadletter(
            ws_id,
            name,
            older_than_seconds=older_than_seconds,
        )
        get_logger().info(
            "workspace.channel.deadletter.purged",
            workspace_id=ws_id,
            channel=name,
            older_than_seconds=older_than_seconds,
            purged=purged,
        )
        return PurgeDLQResult(purged=purged)

    @app.get(
        "/v1/workspaces/{ws_id}/channels/{name}",
        response_model=Channel,
        tags=["channels"],
    )
    async def get_channel(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
    ) -> Channel:
        return await channels.get_channel(ws_id, name)

    @app.delete(
        "/v1/workspaces/{ws_id}/channels/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["channels"],
    )
    async def delete_channel(
        ws_id: str,
        name: str,
        channels: ChannelsDep,
    ) -> Response:
        await channels.delete_channel(ws_id, name)
        get_logger().info(
            "workspace.channel.deleted", workspace_id=ws_id, channel=name
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ workflows

    @app.post(
        "/v1/workspaces/{ws_id}/workflows",
        response_model=Workflow,
        status_code=status.HTTP_201_CREATED,
        tags=["workflows"],
    )
    async def create_workflow(
        ws_id: str,
        body: WorkflowCreate,
        workflows: WorkflowsDep,
        quotas: QuotaEnforcerDep,
        request: Request,
    ) -> Workflow:
        tenant_id = getattr(request.state, "tenant_id", "default")
        # v1.0 — per-workspace workflow quota check (no-op when disabled).
        await quotas.check_workflow_create(tenant_id, ws_id)
        wf = await workflows.create_workflow(
            ws_id,
            body.name,
            body.steps,
            metadata=body.metadata,
        )
        get_logger().info(
            "workspace.workflow.created",
            workspace_id=ws_id,
            workflow_id=wf.id,
            name=wf.name,
            steps=wf.steps_manifest,
        )
        return wf

    @app.get(
        "/v1/workspaces/{ws_id}/workflows",
        response_model=WorkflowList,
        tags=["workflows"],
    )
    async def list_workflows(
        ws_id: str,
        workflows: WorkflowsDep,
    ) -> WorkflowList:
        return WorkflowList(workflows=await workflows.list_workflows(ws_id))

    @app.get(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/resume",
        response_model=ResumeInfo,
        tags=["workflows"],
    )
    async def resume_workflow(
        ws_id: str,
        wf_id: str,
        workflows: WorkflowsDep,
    ) -> ResumeInfo:
        return await workflows.resume_info(ws_id, wf_id)

    @app.get(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}",
        response_model=Workflow,
        tags=["workflows"],
    )
    async def get_workflow(
        ws_id: str,
        wf_id: str,
        workflows: WorkflowsDep,
    ) -> Workflow:
        return await workflows.get_workflow(ws_id, wf_id)

    @app.post(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/steps",
        response_model=WorkflowStep,
        status_code=status.HTTP_201_CREATED,
        tags=["workflows"],
    )
    async def create_workflow_step(
        ws_id: str,
        wf_id: str,
        body: WorkflowStepCreate,
        workflows: WorkflowsDep,
    ) -> WorkflowStep:
        step = await workflows.create_step(
            ws_id,
            wf_id,
            body.name,
            snapshot_id=body.snapshot_id,
            input_=body.input,
            initial_status=body.initial_status,
            max_attempts=body.max_attempts,
            retry_policy=body.retry_policy,
            retry_initial_delay_seconds=body.retry_initial_delay_seconds,
            retry_max_delay_seconds=body.retry_max_delay_seconds,
            retry_jitter=body.retry_jitter,
        )
        get_logger().info(
            "workspace.workflow.step.started",
            workspace_id=ws_id,
            workflow_id=wf_id,
            step_id=step.id,
            step_name=step.name,
            attempt=step.attempt,
            max_attempts=step.max_attempts,
            retry_policy=step.retry_policy,
        )
        return step

    @app.patch(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}",
        response_model=WorkflowStep,
        tags=["workflows"],
    )
    async def update_workflow_step(
        ws_id: str,
        wf_id: str,
        step_id: str,
        body: WorkflowStepUpdate,
        workflows: WorkflowsDep,
    ) -> WorkflowStep:
        step = await workflows.update_step(
            ws_id,
            wf_id,
            step_id,
            status=body.status,
            output=body.output,
            error=body.error,
            snapshot_id=body.snapshot_id,
        )
        get_logger().info(
            "workspace.workflow.step.updated",
            workspace_id=ws_id,
            workflow_id=wf_id,
            step_id=step.id,
            status=step.status,
        )
        return step

    @app.post(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/cancel",
        response_model=Workflow,
        tags=["workflows"],
    )
    async def cancel_workflow(
        ws_id: str,
        wf_id: str,
        workflows: WorkflowsDep,
    ) -> Workflow:
        wf = await workflows.cancel_workflow(ws_id, wf_id)
        get_logger().info(
            "workspace.workflow.cancelled",
            workspace_id=ws_id,
            workflow_id=wf_id,
        )
        return wf

    # ------------------------------------------------------------------ DLQ (v1.1)

    @app.get(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/dlq",
        response_model=DLQEntryList,
        tags=["workflows"],
    )
    async def list_workflow_dlq(
        ws_id: str,
        wf_id: str,
        workflows: WorkflowsDep,
    ) -> DLQEntryList:
        entries = await workflows.list_dlq(ws_id, wf_id)
        return DLQEntryList(entries=entries)

    @app.post(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/dlq/{dlq_id}/replay",
        response_model=DLQReplayResult,
        tags=["workflows"],
    )
    async def replay_workflow_dlq(
        ws_id: str,
        wf_id: str,
        dlq_id: str,
        workflows: WorkflowsDep,
    ) -> DLQReplayResult:
        step = await workflows.replay_dlq(ws_id, wf_id, dlq_id)
        # Replay is two phases: create the new step, then drop the DLQ
        # row. If create_step raised we never reach this line and the
        # DLQ row stays — operator can re-try the replay.
        await workflows.delete_replayed_dlq(wf_id, dlq_id)
        get_logger().info(
            "workspace.workflow.dlq.replayed",
            workspace_id=ws_id,
            workflow_id=wf_id,
            dlq_id=dlq_id,
            new_step_id=step.id,
            step_name=step.name,
        )
        return DLQReplayResult(dlq_id=dlq_id, replayed_step=step)

    @app.delete(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/dlq/{dlq_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["workflows"],
    )
    async def delete_workflow_dlq(
        ws_id: str,
        wf_id: str,
        dlq_id: str,
        workflows: WorkflowsDep,
    ) -> Response:
        await workflows.delete_dlq(ws_id, wf_id, dlq_id)
        get_logger().info(
            "workspace.workflow.dlq.deleted",
            workspace_id=ws_id,
            workflow_id=wf_id,
            dlq_id=dlq_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ leases (v0.5)

    @app.post(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/lease",
        response_model=Lease,
        tags=["leases"],
    )
    async def acquire_step_lease(
        ws_id: str,
        wf_id: str,
        step_id: str,
        body: LeaseAcquireBody,
        leases: LeasesDep,
    ) -> Lease:
        lease = await leases.acquire_lease(
            ws_id,
            wf_id,
            step_id,
            worker_id=body.worker_id,
            ttl_seconds=body.ttl_seconds,
        )
        get_logger().info(
            "workspace.lease.acquired",
            workspace_id=ws_id,
            workflow_id=wf_id,
            step_id=step_id,
            worker_id=body.worker_id,
            ttl=body.ttl_seconds,
        )
        return lease

    @app.post(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/heartbeat",
        response_model=Lease,
        tags=["leases"],
    )
    async def heartbeat_step_lease(
        ws_id: str,
        wf_id: str,
        step_id: str,
        body: LeaseHeartbeatBody,
        leases: LeasesDep,
    ) -> Lease:
        return await leases.heartbeat_lease(
            ws_id,
            wf_id,
            step_id,
            worker_id=body.worker_id,
            ttl_seconds=body.ttl_seconds,
        )

    @app.post(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/release",
        response_model=Lease,
        tags=["leases"],
    )
    async def release_step_lease(
        ws_id: str,
        wf_id: str,
        step_id: str,
        body: LeaseReleaseBody,
        leases: LeasesDep,
    ) -> Lease:
        lease = await leases.release_lease(
            ws_id,
            wf_id,
            step_id,
            worker_id=body.worker_id,
            step_status=body.status,
            output=body.output,
            error=body.error,
            snapshot_id=body.snapshot_id,
        )
        get_logger().info(
            "workspace.lease.released",
            workspace_id=ws_id,
            workflow_id=wf_id,
            step_id=step_id,
            worker_id=body.worker_id,
            step_status=body.status,
        )
        return lease

    @app.get(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/pending",
        response_model=WorkflowStepList,
        tags=["leases"],
    )
    async def list_pending_steps(
        ws_id: str,
        wf_id: str,
        leases: LeasesDep,
    ) -> WorkflowStepList:
        rows = await leases.list_pending_steps(ws_id, wf_id)
        return WorkflowStepList(steps=[_row_to_step(r) for r in rows])

    @app.get(
        "/v1/workspaces/{ws_id}/workflows/{wf_id}/expired",
        response_model=LeaseList,
        tags=["leases"],
    )
    async def list_expired_leases(
        ws_id: str,
        wf_id: str,
        leases: LeasesDep,
    ) -> LeaseList:
        return LeaseList(leases=await leases.list_expired_leases(ws_id, wf_id))

    # ------------------------------------------------------------------ workers (v0.5)

    @app.post(
        "/v1/workers/register",
        response_model=Worker,
        status_code=status.HTTP_201_CREATED,
        tags=["workers"],
    )
    async def register_worker(
        body: WorkerRegistration,
        leases: LeasesDep,
    ) -> Worker:
        worker = await leases.register_worker(hostname=body.hostname, pid=body.pid)
        get_logger().info(
            "workspace.worker.registered",
            worker_id=worker.id,
            hostname=worker.hostname,
            pid=worker.pid,
        )
        return worker

    @app.post(
        "/v1/workers/{worker_id}/heartbeat",
        response_model=Worker,
        tags=["workers"],
    )
    async def heartbeat_worker(
        worker_id: str,
        leases: LeasesDep,
    ) -> Worker:
        return await leases.heartbeat_worker(worker_id)

    @app.post(
        "/v1/workers/{worker_id}/drain",
        response_model=Worker,
        tags=["workers"],
    )
    async def drain_worker(
        worker_id: str,
        leases: LeasesDep,
    ) -> Worker:
        worker = await leases.drain_worker(worker_id)
        get_logger().info("workspace.worker.draining", worker_id=worker_id)
        return worker

    @app.get(
        "/v1/workers",
        response_model=WorkerList,
        tags=["workers"],
    )
    async def list_workers(
        leases: LeasesDep,
        worker_status: Annotated[Optional[str], Query(alias="status")] = None,  # noqa: UP007, UP045
    ) -> WorkerList:
        return WorkerList(workers=await leases.list_workers(status=worker_status))

    # ------------------------------------------------------------------ locks (v0.6)
    #
    # Generic distributed locks over named workspace resources. The ``name``
    # uses ``:path`` so callers can use ``/`` as a structuring separator
    # (e.g. ``kv:sources/index``) without manual URL-escaping. The list
    # endpoint is registered before the ``{name:path}`` routes so FastAPI's
    # router prefers the static segment when both could match.

    @app.get(
        "/v1/workspaces/{ws_id}/locks",
        response_model=LockList,
        tags=["locks"],
    )
    async def list_locks(
        ws_id: str,
        locks: ResourceLocksDep,
        store: StoreDep,
        request: Request,
    ) -> LockList:
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)
        return LockList(locks=await locks.list_locks(ws_id))

    @app.post(
        "/v1/workspaces/{ws_id}/locks/{name:path}/acquire",
        response_model=Lock,
        tags=["locks"],
    )
    async def acquire_lock(
        ws_id: str,
        name: str,
        body: LockAcquireBody,
        locks: ResourceLocksDep,
        store: StoreDep,
        request: Request,
    ) -> Lock:
        if not name:
            raise InvalidArguments("lock name must be non-empty")
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)

        lock = await locks.acquire(
            ws_id,
            name,
            holder=body.holder,
            ttl_seconds=body.ttl_seconds,
            wait_ms=body.wait_ms,
        )
        get_logger().info(
            "workspace.lock.acquired",
            workspace_id=ws_id,
            name=name,
            holder=body.holder,
            ttl=body.ttl_seconds,
            wait_ms=body.wait_ms,
        )
        return lock

    @app.post(
        "/v1/workspaces/{ws_id}/locks/{name:path}/heartbeat",
        response_model=Lock,
        tags=["locks"],
    )
    async def heartbeat_lock(
        ws_id: str,
        name: str,
        body: LockHeartbeatBody,
        locks: ResourceLocksDep,
        store: StoreDep,
        request: Request,
    ) -> Lock:
        if not name:
            raise InvalidArguments("lock name must be non-empty")
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)

        return await locks.heartbeat(
            ws_id,
            name,
            holder=body.holder,
            ttl_seconds=body.ttl_seconds,
        )

    @app.post(
        "/v1/workspaces/{ws_id}/locks/{name:path}/release",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["locks"],
    )
    async def release_lock(
        ws_id: str,
        name: str,
        body: LockReleaseBody,
        locks: ResourceLocksDep,
        store: StoreDep,
        request: Request,
    ) -> Response:
        if not name:
            raise InvalidArguments("lock name must be non-empty")
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)

        await locks.release(ws_id, name, holder=body.holder)
        get_logger().info(
            "workspace.lock.released",
            workspace_id=ws_id,
            name=name,
            holder=body.holder,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/workspaces/{ws_id}/locks/{name:path}",
        response_model=Lock,
        tags=["locks"],
    )
    async def get_lock(
        ws_id: str,
        name: str,
        locks: ResourceLocksDep,
        store: StoreDep,
        request: Request,
    ) -> Lock:
        if not name:
            raise InvalidArguments("lock name must be non-empty")
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)
        lock = await locks.get(ws_id, name)
        if lock is None:
            from .exceptions import LockNotFound

            raise LockNotFound(ws_id, name)
        return lock

    # ------------------------------------------------------------------ retention / GC

    @app.get(
        "/v1/workspaces/{ws_id}/retention",
        response_model=RetentionPolicy,
        tags=["gc"],
    )
    async def get_retention(
        ws_id: str,
        store: StoreDep,
        retention: RetentionDep,
        request: Request,
    ) -> RetentionPolicy:
        tenant_id = getattr(request.state, "tenant_id", "default")
        # Visibility check — same 404 the GET workspace endpoint returns.
        await store.get_workspace(ws_id, tenant_id=tenant_id)
        return await retention.get(ws_id)

    @app.put(
        "/v1/workspaces/{ws_id}/retention",
        response_model=RetentionPolicy,
        tags=["gc"],
    )
    async def put_retention(
        ws_id: str,
        body: RetentionPolicyUpdate,
        store: StoreDep,
        retention: RetentionDep,
        request: Request,
    ) -> RetentionPolicy:
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)
        policy = await retention.upsert(
            ws_id,
            keep_versions=body.keep_versions,
            keep_days=body.keep_days,
            keep_snapshots=body.keep_snapshots,
            delete_unreferenced_blobs=body.delete_unreferenced_blobs,
        )
        get_logger().info(
            "workspace.retention.updated",
            workspace_id=ws_id,
            keep_versions=policy.keep_versions,
            keep_days=policy.keep_days,
            keep_snapshots=policy.keep_snapshots,
        )
        return policy

    @app.post(
        "/v1/workspaces/{ws_id}/gc",
        response_model=GCResult,
        tags=["gc"],
    )
    async def run_gc(
        ws_id: str,
        store: StoreDep,
        retention: RetentionDep,
        gc_engine: GCEngineDep,
        request: Request,
    ) -> GCResult:
        tenant_id = getattr(request.state, "tenant_id", "default")
        await store.get_workspace(ws_id, tenant_id=tenant_id)
        policy = await retention.get(ws_id)
        result = await gc_engine.run(ws_id, policy)
        get_logger().info(
            "workspace.gc.completed",
            workspace_id=ws_id,
            kv_versions_deleted=result.kv_versions_deleted,
            file_versions_deleted=result.file_versions_deleted,
            blob_files_deleted=result.blob_files_deleted,
            snapshots_deleted=result.snapshots_deleted,
            branches_deleted=result.branches_deleted,
            bytes_freed=result.bytes_freed,
            duration_ms=result.duration_ms,
        )
        return result

    @app.post(
        "/v1/admin/gc",
        response_model=GCResultList,
        tags=["gc"],
    )
    async def admin_gc(
        store: StoreDep,
        retention: RetentionDep,
        gc_engine: GCEngineDep,
        request: Request,
    ) -> GCResultList:
        # Admin-scope check. ``tenant:*:admin`` (any tenant admin) or ``*``
        # (superuser) are accepted.
        scopes = list(getattr(request.state, "auth_scopes", []) or [])
        permitted = "*" in scopes or "tenant:*:admin" in scopes
        # Permissive (no token) deployments need a way to call this in dev:
        # accept when auth is permissive AND auth_required is off — same
        # bar as every other admin-y action in v0.3+.
        settings: Settings = request.app.state.settings
        if (
            settings.auth_mode == "permissive"
            and not settings.auth_required
        ):
            permitted = True
        if not permitted:
            raise Unauthorized(
                "admin sweep requires tenant:*:admin or * scope",
                code="UNAUTHORIZED",
                details={"required_scope": "tenant:*:admin"},
            )

        results: list[GCResult] = []
        for ws_id in await retention.workspaces_with_policies():
            try:
                ws = await store.get_workspace(ws_id)
            except Exception:  # pragma: no cover -- defensive
                continue
            policy = await retention.get(ws.id)
            result = await gc_engine.run(ws.id, policy)
            results.append(result)
        get_logger().info(
            "workspace.admin.gc.completed",
            count=len(results),
        )
        return GCResultList(results=results)

    # ------------------------------------------------------------------ load-shed

    @app.get(
        "/v1/admin/load-shed/stats",
        tags=["admin"],
    )
    async def load_shed_stats(request: Request) -> dict:
        """Return current load-shed counters.

        Permissive in dev (no token, no admin scope required) for the same
        reason ``/v1/admin/gc`` is — operators want to introspect the
        shedder during a benchmark run without rolling a token.

        In strict-auth deployments, callers need ``tenant:*:admin`` or
        ``*``.
        """

        settings: Settings = request.app.state.settings
        permitted = (
            settings.auth_mode == "permissive" and not settings.auth_required
        )
        if not permitted:
            scopes = list(getattr(request.state, "auth_scopes", []) or [])
            if "*" in scopes or "tenant:*:admin" in scopes:
                permitted = True
        if not permitted:
            raise Unauthorized(
                "load-shed stats require tenant:*:admin or * scope",
                code="UNAUTHORIZED",
                details={"required_scope": "tenant:*:admin"},
            )
        return request.app.state.load_shedder.stats

    # ----------------------------------------------------------- revocation cache

    @app.get(
        "/v1/admin/revocations/cache/stats",
        tags=["admin"],
    )
    async def revocation_cache_stats(request: Request) -> dict:
        """Return current revocation-cache counters.

        Permissive in dev (no token / no admin scope required) for the
        same reason ``/v1/admin/load-shed/stats`` is. In strict-auth
        deployments, callers need ``tenant:*:admin`` or ``*``.
        """

        settings: Settings = request.app.state.settings
        permitted = (
            settings.auth_mode == "permissive" and not settings.auth_required
        )
        if not permitted:
            scopes = list(getattr(request.state, "auth_scopes", []) or [])
            if "*" in scopes or "tenant:*:admin" in scopes:
                permitted = True
        if not permitted:
            raise Unauthorized(
                "revocation cache stats require tenant:*:admin or * scope",
                code="UNAUTHORIZED",
                details={"required_scope": "tenant:*:admin"},
            )
        rev_cache: RevocationCache = request.app.state.revocation_cache
        return rev_cache.stats

    # ------------------------------------------------------------------ regions

    @app.get(
        "/v1/regions",
        response_model=RegionsResponse,
        tags=["meta"],
    )
    async def get_regions(request: Request) -> RegionsResponse:
        """Return the current region + cached peer reachability."""

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

    # ------------------------------------------------------------------ replication

    @app.get(
        "/v1/admin/replication/log",
        tags=["replication"],
    )
    async def replication_log_fetch(
        request: Request,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=10000),
    ) -> dict:
        """Return mutation entries with ``seq > since`` (capped by ``limit``)."""

        _require_admin(request)
        log_store: ReplicationLog = request.app.state.replication_log
        entries = await log_store.fetch(since=since, limit=limit)
        return {
            "entries": [entry.to_dict() for entry in entries],
            "next_since": entries[-1].seq if entries else since,
        }

    @app.get(
        "/v1/admin/replication/status",
        tags=["replication"],
    )
    async def replication_status(request: Request) -> dict:
        """Return the local replication mode + current sequence."""

        _require_admin(request)
        settings: Settings = request.app.state.settings
        log_store: ReplicationLog = request.app.state.replication_log
        current_seq = await log_store.current_seq()
        probe: RegionStatusProbe = request.app.state.region_status_probe
        peer_urls = {
            peer_id: settings.region_peer_urls.get(peer_id, "")
            for peer_id in settings.region_peers
            if settings.region_peer_urls.get(peer_id)
        }
        peers = await probe.all_peers(peer_urls) if peer_urls else []
        peers_lag = {p.id: p.lag_ms for p in peers if p.lag_ms is not None}
        return {
            "mode": settings.replication_mode,
            "region": settings.region_id,
            "current_seq": current_seq,
            "peers_lag": peers_lag,
        }

    @app.post(
        "/v1/admin/replication/apply",
        status_code=status.HTTP_201_CREATED,
        tags=["replication"],
    )
    async def replication_apply(
        request: Request,
        body: list[dict],
    ) -> dict:
        """Ingest mutation entries from a primary peer.

        Skips entries whose ``seq`` is already present locally so a replica
        can safely retry an apply call without duplicating state.
        """

        _require_admin(request)
        log_store: ReplicationLog = request.app.state.replication_log
        applied, skipped = await log_store.apply_entries(body)
        return {"applied": applied, "skipped": skipped}

    # ------------------------------------------------------------------ migrations

    @app.get(
        "/v1/admin/migrations",
        tags=["migrations"],
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
        status_code=status.HTTP_201_CREATED,
        tags=["migrations"],
    )
    async def apply_migrations(request: Request) -> JSONResponse:
        _require_admin(request)
        runner: MigrationRunner = request.app.state.migration_runner
        try:
            applied_migs = await runner.apply_pending(blocking_lock=False)
        except MigrationLockError as exc:
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
        return JSONResponse(
            status_code=201,
            content={
                "applied": [_serialize_applied(m) for m in applied_migs],
            },
        )

    @app.post(
        "/v1/admin/migrations/rollback",
        response_model=RollbackResult,
        tags=["migrations"],
    )
    async def rollback_migrations(
        body: RollbackBody,
        request: Request,
    ) -> RollbackResult:
        _require_admin(request)
        runner: MigrationRunner = request.app.state.migration_runner
        try:
            outcome = await runner.rollback_to(
                body.to,
                dry_run=body.dry_run,
                blocking_lock=False,
            )
        except RunnerRollbackMissing as exc:
            raise MigrationRollbackMissing(exc.missing_ids) from exc
        except MigrationLockError as exc:
            return JSONResponse(  # type: ignore[return-value]
                status_code=409,
                content={
                    "error": {
                        "code": "MIGRATION_LOCKED",
                        "message": str(exc),
                        "details": {},
                    }
                },
            )

        # The runner returns a successful outcome even when one of the
        # rollbacks errored (so the caller sees how many committed). When
        # ``failed`` is set we still return 200 with that information so
        # the client can decide what to do — same pattern as ``apply``
        # returns 201 with the partial list when no error, and like the
        # transactions endpoint reports per-call status.
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

    # ---------------------------------------------- v1.0 GDPR (admin endpoints)
    #
    # Tenant-scoped export + delete. Called by the identity service when
    # orchestrating a GDPR Article 20 (portability) or Article 17 (erasure)
    # request. Both endpoints require admin scope (or no auth in dev mode).

    @app.get(
        "/v1/admin/tenant/{tenant_id}/export-data",
        tags=["admin"],
    )
    async def admin_export_tenant_data(
        tenant_id: str,
        request: Request,
        store: StoreDep,
    ) -> StarletteResponse:
        """Stream every tenant-scoped row as JSON-Lines.

        Body content-type is ``application/jsonl``. Each line is a single
        JSON object with a ``type`` field (``workspace``, ``kv_entry``,
        ``file_entry``, ``snapshot``, ``branch``, ``channel``,
        ``channel_message``, ``workflow``, ``workflow_step``, etc.).
        """

        _require_admin(request)
        compliance = WorkspaceComplianceStore(
            store.db_path, store.blobs_dir
        )
        lines: list[str] = []
        async for line in compliance.export_jsonl(tenant_id):
            lines.append(line)
        body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        return StarletteResponse(
            content=body,
            media_type="application/jsonl",
        )

    @app.delete(
        "/v1/admin/tenant/{tenant_id}/data",
        tags=["admin"],
    )
    async def admin_delete_tenant_data(
        tenant_id: str,
        request: Request,
        store: StoreDep,
    ) -> dict:
        """Hard-delete every tenant-scoped row (workspace cascade)."""

        _require_admin(request)
        compliance = WorkspaceComplianceStore(
            store.db_path, store.blobs_dir
        )
        counts = await compliance.delete_tenant_data(tenant_id)
        get_logger().info(
            "workspace.admin.gdpr_delete",
            tenant_id=tenant_id,
            counts=counts,
        )
        return {"deleted": counts}


# Helpers for the admin/migrations endpoints. Plain ``dict`` returns avoid
# adding pydantic models that mirror ``MigrationRunner`` dataclasses 1:1.


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
    """Permissive (dev) deployments accept any caller; strict modes need scope."""

    settings: Settings = request.app.state.settings
    if settings.auth_mode == "permissive" and not settings.auth_required:
        return
    scopes = list(getattr(request.state, "auth_scopes", []) or [])
    if "*" in scopes or "tenant:*:admin" in scopes:
        return
    raise Unauthorized(
        "admin migrations require tenant:*:admin or * scope",
        code="UNAUTHORIZED",
        details={"required_scope": "tenant:*:admin"},
    )


async def _refresh_workspace_gauges(app: FastAPI, registry: MetricsRegistry) -> None:
    """Refresh workspace-specific gauges on each Prometheus scrape.

    Best-effort: any failure leaves the previously-set value in place. A
    bad SELECT here must NEVER cause the scrape itself to fail. Each
    ``try``/``except`` is intentionally narrow: we want to keep refreshing
    the *other* gauges even if one of them blows up against an unfamiliar
    schema.
    """

    # Mirror load-shedder cumulative count into a Prometheus counter.
    shedder: LoadShedder | None = getattr(app.state, "load_shedder", None)
    if shedder is not None:
        shed_count = float(shedder.stats.get("shed_count", 0) or 0)
        c = registry.counter(
            "plinth_load_shed_total",
            {"service": registry.service_name},
        )
        delta = shed_count - c.value
        if delta > 0:
            c.inc(delta)

    store = getattr(app.state, "store", None)
    workflows = getattr(app.state, "workflows", None)
    leases = getattr(app.state, "leases", None)
    if store is None:
        return

    # Per-tenant workspace count.
    tenants: list = []
    try:
        tenants = await store.list_tenants()
        for t in tenants:
            registry.gauge(
                "plinth_workspaces_total",
                {"tenant_id": str(t.get("id", "default"))},
            ).set(int(t.get("workspace_count", 0) or 0))
    except Exception:  # noqa: BLE001
        pass

    # Per-tenant total stored bytes (optional; method may not exist in tests).
    storage_fn = getattr(store, "tenant_storage_bytes", None)
    if callable(storage_fn) and tenants:
        for t in tenants:
            tenant_id = str(t.get("id", "default"))
            try:
                bytes_used = await storage_fn(tenant_id)
                registry.gauge(
                    "plinth_storage_bytes",
                    {"tenant_id": tenant_id},
                ).set(int(bytes_used or 0))
            except Exception:  # noqa: BLE001
                continue

    # Active workflows + workflow steps grouped by state.
    if workflows is not None and tenants:
        count_fn = getattr(workflows, "count_active", None)
        if callable(count_fn):
            for t in tenants:
                tenant_id = str(t.get("id", "default"))
                try:
                    n = await count_fn(tenant_id=tenant_id)
                    registry.gauge(
                        "plinth_workflows_active",
                        {"tenant_id": tenant_id},
                    ).set(int(n or 0))
                except Exception:  # noqa: BLE001
                    continue
        steps_fn = getattr(workflows, "count_steps_by_state", None)
        if callable(steps_fn):
            try:
                grouped = await steps_fn()
                for state, count in (grouped or {}).items():
                    registry.gauge(
                        "plinth_workflow_steps_total",
                        {"state": str(state)},
                    ).set(int(count or 0))
            except Exception:  # noqa: BLE001
                pass

    # Active workers — ``LeaseStore`` exposes ``count_active_workers``.
    if leases is not None:
        active_fn = getattr(leases, "count_active_workers", None)
        if callable(active_fn):
            try:
                n = await active_fn()
                registry.gauge(
                    "plinth_workers_active",
                    {"service": registry.service_name},
                ).set(int(n or 0))
            except Exception:  # noqa: BLE001
                pass


# A module-level default for environments that import ``plinth_workspace.api:app``.
# Lazy: built only on first attribute access so tests that import
# ``create_app`` don't pay startup cost.

_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


# Re-export for type checkers and direct uvicorn imports.
__all__ = ["create_app"]
