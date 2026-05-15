"""
Spike 0.1 — FastAPI Sub-App Lifespan behavior under three architectures.

Question: When five Plinth services need to live in one process (Embedded
Mode), which mounting strategy preserves their lifespan startup hooks
(DB pool init, OAuth-token-decryption setup, background tasks)?

Three candidates tested:
  A) Raw mount      — root.mount("/_svc", sub_app) — known to skip lifespan
  B) AsyncExitStack — root has a custom lifespan that manually enters every
                      sub-app's lifespan context
  C) APIRouter      — services export a router; embedded app does
                      root.include_router(router, prefix="/_svc")

Each candidate runs a tiny FastAPI sub-app whose lifespan flips a flag in a
shared dict. Test asserts the flag is True after the root app is started.

Run:
    pip install fastapi uvicorn httpx pytest pytest-asyncio
    pytest spike/test_embedded_lifespan.py -v
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

# NOTE: httpx.ASGITransport does NOT send ASGI lifespan events. To test
# lifespan behavior we must wrap apps in asgi_lifespan.LifespanManager,
# which drives the lifespan protocol the same way uvicorn does at server
# startup. This is the canonical pattern for async lifespan unit tests.


# ---------------------------------------------------------------------------
# Shared test fixtures: a flag dict that each lifespan flips.
# Modules under test will read/write the same dict.
# ---------------------------------------------------------------------------


def make_sub_app(name: str, flags: dict[str, bool]) -> FastAPI:
    """A FastAPI sub-app whose lifespan flips flags[name] to True."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        flags[f"{name}_started"] = True
        yield
        flags[f"{name}_stopped"] = True

    app = FastAPI(lifespan=lifespan)

    @app.get("/state")
    async def state() -> dict[str, bool]:
        return dict(flags)

    return app


def make_sub_router(name: str, flags: dict[str, bool]) -> APIRouter:
    """APIRouter variant — no lifespan, but startup hook via on_event."""
    router = APIRouter()

    @router.get("/state")
    async def state() -> dict[str, bool]:
        return dict(flags)

    return router


# ---------------------------------------------------------------------------
# Candidate A — Raw mount. Expected: sub-app lifespan is NOT called.
# Reproduces Starlette #649.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A_raw_mount_skips_lifespan() -> None:
    flags: dict[str, bool] = {}
    ws_app = make_sub_app("workspace", flags)
    gw_app = make_sub_app("gateway", flags)

    root = FastAPI()
    root.mount("/_ws", ws_app)
    root.mount("/_gw", gw_app)

    # LifespanManager drives root lifespan as uvicorn would.
    async with LifespanManager(root):
        transport = ASGITransport(app=root)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/_ws/state")

    # The CRITICAL assertion: even though the ROOT app went through lifespan,
    # the MOUNTED sub-apps did not. This is Starlette #649.
    assert resp.status_code == 200
    state = resp.json()
    print(f"\n[Candidate A — raw mount] state after request: {state}")
    workspace_started = state.get("workspace_started", False)
    gateway_started = state.get("gateway_started", False)
    assert not workspace_started, "Workspace sub-app lifespan unexpectedly fired"
    assert not gateway_started, "Gateway sub-app lifespan unexpectedly fired"


# ---------------------------------------------------------------------------
# Candidate B — AsyncExitStack on root lifespan. Manually enter each sub-app's
# lifespan via Starlette's internal LifespanContextManager.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B_async_exit_stack_runs_lifespans() -> None:
    flags: dict[str, bool] = {}
    ws_app = make_sub_app("workspace", flags)
    gw_app = make_sub_app("gateway", flags)

    sub_apps = [ws_app, gw_app]

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for sub in sub_apps:
                # Each FastAPI app exposes its lifespan via .router.lifespan_context
                lifespan_ctx = sub.router.lifespan_context
                await stack.enter_async_context(lifespan_ctx(sub))
            yield

    root = FastAPI(lifespan=combined_lifespan)
    root.mount("/_ws", ws_app)
    root.mount("/_gw", gw_app)

    async with LifespanManager(root):
        transport = ASGITransport(app=root)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/_ws/state")

    assert resp.status_code == 200
    state = resp.json()
    print(f"\n[Candidate B — AsyncExitStack] state after request: {state}")
    assert state.get("workspace_started"), "Workspace lifespan did not fire"
    assert state.get("gateway_started"), "Gateway lifespan did not fire"


# ---------------------------------------------------------------------------
# Candidate C — APIRouter inclusion. Services export a router; embedded app
# uses include_router. No nested lifespan — startup logic moves to root.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_apirouter_centralizes_lifespan() -> None:
    flags: dict[str, bool] = {}
    ws_router = make_sub_router("workspace", flags)
    gw_router = make_sub_router("gateway", flags)

    # Each service exposes a startup function the root composer calls.
    async def ws_startup() -> None:
        flags["workspace_started"] = True

    async def gw_startup() -> None:
        flags["gateway_started"] = True

    @asynccontextmanager
    async def root_lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ws_startup()
        await gw_startup()
        yield
        flags["workspace_stopped"] = True
        flags["gateway_stopped"] = True

    root = FastAPI(lifespan=root_lifespan)
    root.include_router(ws_router, prefix="/_ws")
    root.include_router(gw_router, prefix="/_gw")

    async with LifespanManager(root):
        transport = ASGITransport(app=root)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/_ws/state")

    assert resp.status_code == 200
    state = resp.json()
    print(f"\n[Candidate C — APIRouter] state after request: {state}")
    assert state.get("workspace_started"), "Workspace startup did not fire"
    assert state.get("gateway_started"), "Gateway startup did not fire"


# ---------------------------------------------------------------------------
# Candidate D (bonus) — Hybrid: mount + AsyncExitStack composing existing
# FastAPI sub-apps without refactoring them to routers.
# Same as B but written more explicitly so we can measure complexity cost.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_D_hybrid_mount_with_explicit_lifespan_dispatch() -> None:
    """
    Real-world variant: each service still exposes a `create_app()` factory
    that returns a full FastAPI app (so standalone deployments keep working).
    Embedded mode mounts these and dispatches their lifespan via a registry.
    """
    flags: dict[str, bool] = {}

    def create_workspace_app() -> FastAPI:
        return make_sub_app("workspace", flags)

    def create_gateway_app() -> FastAPI:
        return make_sub_app("gateway", flags)

    services: list[tuple[str, FastAPI]] = [
        ("workspace", create_workspace_app()),
        ("gateway", create_gateway_app()),
    ]

    @asynccontextmanager
    async def dispatched_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for name, sub in services:
                ctx = sub.router.lifespan_context(sub)
                await stack.enter_async_context(ctx)
                # Verify it fired before yielding
                assert flags.get(f"{name}_started"), f"{name} did not start"
            yield

    root = FastAPI(lifespan=dispatched_lifespan)
    for name, sub in services:
        root.mount(f"/_{name}", sub)

    async with LifespanManager(root):
        transport = ASGITransport(app=root)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/_workspace/state")

    state = resp.json()
    print(f"\n[Candidate D — hybrid] state after request: {state}")
    assert state.get("workspace_started")
    assert state.get("gateway_started")


# ---------------------------------------------------------------------------
# Realism stress test — simulate cross-service in-process HTTP call via
# httpx ASGITransport, as the real Embedded Mode will need (Gateway → Identity).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E_cross_service_call_via_asgi_transport() -> None:
    """
    Embedded mode must replace HTTP-based service-to-service calls with
    in-process ASGI calls. Verify the pattern works with the chosen mount
    strategy (Candidate B/D).
    """
    flags: dict[str, bool] = {"identity_started": False}

    @asynccontextmanager
    async def identity_lifespan(app: FastAPI) -> AsyncIterator[None]:
        flags["identity_started"] = True
        yield

    identity_app = FastAPI(lifespan=identity_lifespan)

    @identity_app.get("/v1/verify")
    async def verify() -> dict[str, str]:
        return {"sub": "anna", "tenant": "default"}

    # Gateway service that needs to call identity in-process.
    gateway_app = FastAPI()
    # Inject identity client via dependency at app level
    gateway_app.state.identity_transport = ASGITransport(app=identity_app)

    @gateway_app.get("/route")
    async def route() -> dict[str, str]:
        async with AsyncClient(
            transport=gateway_app.state.identity_transport,
            base_url="http://identity",
        ) as identity:
            resp = await identity.get("/v1/verify")
            data = resp.json()
        return {"routed_for": data["sub"]}

    @asynccontextmanager
    async def root_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                identity_app.router.lifespan_context(identity_app)
            )
            yield

    root = FastAPI(lifespan=root_lifespan)
    root.mount("/_identity", identity_app)
    root.mount("/_gateway", gateway_app)

    async with LifespanManager(root):
        transport = ASGITransport(app=root)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/_gateway/route")

    print(f"\n[Candidate E — cross-service ASGI] response: {resp.json()}, flags: {flags}")
    assert resp.status_code == 200
    assert resp.json() == {"routed_for": "anna"}
    assert flags["identity_started"], "Identity lifespan must run before gateway calls it"
