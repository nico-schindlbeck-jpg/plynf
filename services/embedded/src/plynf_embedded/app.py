"""Embedded app composer.

Wires together the five core services into a single FastAPI app, with
an AsyncExitStack-driven lifespan that fires each sub-app's startup
hooks (DB pools, key derivation, background tasks). See ADR 0009.

URL layout under the unified port 7420:

    /                       → dashboard SPA (root for browser convenience)
    /_workspace/v1/...      → workspace API (was port 7421)
    /_gateway/v1/...        → tool gateway API (was port 7422)
    /_identity/v1/...       → identity API (was port 7425)
    /_mock/...              → mock MCP (was port 7423)

Cross-service HTTP calls (e.g. gateway → identity for JWT verify) use
httpx.AsyncClient(transport=ASGITransport(app=sibling)). No loopback
TCP. Set up in `_wire_intra_service_clients` below.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

# Sibling service factories. Each one accepts embedded=True to switch
# to single-process mode (no separate uvicorn, shared lifespan).
# These imports may show as red in a fresh IDE — the actual service
# packages live in services/{workspace,gateway,identity,dashboard}/src
# and are pip-installed as part of `make install`.
from plinth_workspace.app import create_app as create_workspace_app  # type: ignore
from plinth_gateway.app   import create_app as create_gateway_app    # type: ignore
from plinth_identity.app  import create_app as create_identity_app   # type: ignore
from plinth_dashboard.app import create_app as create_dashboard_app  # type: ignore
from mock_mcp.app         import create_app as create_mock_app       # type: ignore

log = logging.getLogger("plynf.embedded")


def make_embedded_app() -> FastAPI:
    """Construct the embedded-mode root app.

    Order in the services list matters — identity boots first so that
    by the time gateway's startup hook tries to fetch JWKS, the
    identity app's lifespan has already initialized RS256 keys.
    """
    # Build sub-apps with embedded=True flag so they switch to
    # in-process behaviour (shared SQLite path, no own uvicorn).
    identity_app  = create_identity_app(embedded=True)
    workspace_app = create_workspace_app(embedded=True)
    gateway_app   = create_gateway_app(embedded=True)
    mock_app      = create_mock_app(embedded=True)
    dashboard_app = create_dashboard_app(embedded=True)

    # Strict startup order. Identity first — everyone else may need
    # to verify JWTs or fetch JWKS during their own init.
    services: list[tuple[str, FastAPI]] = [
        ("identity",  identity_app),
        ("workspace", workspace_app),
        ("gateway",   gateway_app),
        ("mock",      mock_app),
        ("dashboard", dashboard_app),
    ]

    # ── Intra-service clients (no loopback HTTP) ──────────────────
    # Gateway needs identity for JWT verification. Dashboard needs
    # everything. Wire via httpx ASGITransport so calls stay in-process.
    _wire_intra_service_clients({
        "identity":  identity_app,
        "workspace": workspace_app,
        "gateway":   gateway_app,
    })

    # ── Composed lifespan ─────────────────────────────────────────
    @asynccontextmanager
    async def composed_lifespan(_root: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for name, sub in services:
                log.info("plynf-embedded: starting %s ...", name)
                ctx = sub.router.lifespan_context(sub)
                await stack.enter_async_context(ctx)
                log.info("plynf-embedded: %s ready", name)
            log.info("plynf-embedded: all services up. Listening on :7420")
            yield
            log.info("plynf-embedded: shutting down ...")

    root = FastAPI(
        title="Plynf Embedded",
        version="0.1.0",
        lifespan=composed_lifespan,
        docs_url="/_docs",     # OpenAPI for the unified surface
        redoc_url=None,
    )

    # Mount sub-apps under /_<name>/. Dashboard is exception: it owns "/"
    # so browser hits land on the SPA directly.
    for name, sub in services[:-1]:
        root.mount(f"/_{name}", sub)
    root.mount("/", dashboard_app)

    @root.get("/_health", include_in_schema=False)
    async def _health() -> dict[str, str]:
        return {"status": "ok", "mode": "embedded", "version": "0.1.0"}

    return root


def _wire_intra_service_clients(apps: dict[str, FastAPI]) -> None:
    """Replace HTTP clients for cross-service calls with ASGI transports.

    Each service exposes a settable `http_client_factory` on its app
    state. In Compose-mode this points at a real httpx.AsyncClient over
    loopback HTTP. In embedded mode we override to use ASGITransport,
    which dispatches requests directly to the sibling app's ASGI handler
    — same process, no socket round-trip.

    This is the canonical pattern from ADR 0009 candidate E (test_E in
    spike/test_embedded_lifespan.py). Verified to preserve full FastAPI
    request semantics including headers, cookies, status codes.
    """
    for name, app in apps.items():
        if not hasattr(app.state, "http_client_factory"):
            log.debug(
                "Service %s has no http_client_factory hook; embedded "
                "cross-service calls for it will use the default real HTTP.",
                name,
            )
            continue
        # Build a factory that returns a fresh AsyncClient per caller,
        # transport-bound to a specific peer.
        def make_factory(peer_app: FastAPI):
            def _factory(peer_name: str) -> httpx.AsyncClient:
                # peer_name corresponds to whichever sibling we want to call.
                # In embedded mode we ignore the URL and route via ASGI.
                return httpx.AsyncClient(
                    transport=ASGITransport(app=peer_app),
                    base_url=f"http://{peer_name}",
                )
            return _factory
        app.state.http_client_factory = make_factory(app)
