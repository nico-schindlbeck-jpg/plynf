"""Smoke tests for the embedded composer.

These verify that the composed app structure works end-to-end: lifespan
fires for all sub-apps, requests route to the right service via URL prefix,
and the dashboard SPA is served at root. Cross-service in-process calls
(gateway → identity) are tested separately in test_intra_service.py once
the embedded=True hooks land in sibling services.

Tests use asgi-lifespan to drive lifespan, since httpx.ASGITransport
does NOT send ASGI lifespan events by default. See ADR 0009 for the
gotcha.
"""

from __future__ import annotations

import pytest

# These imports require all sibling services to be installed. Skip
# gracefully so partial-install dev environments can still run the rest
# of the suite.
asgi_lifespan = pytest.importorskip("asgi_lifespan")
httpx = pytest.importorskip("httpx")
try:
    from plynf_embedded.app import make_embedded_app
    EMBEDDED_AVAILABLE = True
except ImportError as e:
    EMBEDDED_AVAILABLE = False
    IMPORT_ERROR = str(e)


pytestmark = pytest.mark.skipif(
    not EMBEDDED_AVAILABLE,
    reason=f"sibling services not installed: {IMPORT_ERROR if not EMBEDDED_AVAILABLE else ''}",
)


@pytest.fixture
async def embedded_client():
    """Yield an httpx client bound to the embedded app with lifespan running."""
    from asgi_lifespan import LifespanManager

    app = make_embedded_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://embedded") as client:
            yield client


# ─── Test 1: Embedded health endpoint ────────────────────────────────


async def test_root_health(embedded_client):
    """The composer adds /_health that confirms embedded mode."""
    resp = await embedded_client.get("/_health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "embedded"


# ─── Test 2: Each sub-app's health endpoint is reachable via prefix ──


@pytest.mark.parametrize(
    "prefix",
    ["/_workspace", "/_gateway", "/_identity", "/_mock"],
)
async def test_subapp_healthz_routes(embedded_client, prefix):
    """Each mounted sub-app responds to its standard /healthz under the prefix."""
    resp = await embedded_client.get(f"{prefix}/healthz")
    # Sub-apps might or might not have lifted off in this test setup
    # (they all need their migrations to run, etc.). Either 200 (ready)
    # or 503 (still booting) is acceptable; 404 means the mount failed.
    assert resp.status_code in (200, 503), (
        f"{prefix}/healthz returned {resp.status_code} — "
        f"sub-app mount may be broken"
    )


# ─── Test 3: Dashboard SPA is mounted at root ────────────────────────


async def test_dashboard_at_root(embedded_client):
    """Browser hits to / should land on dashboard, not a 404."""
    resp = await embedded_client.get("/")
    # Dashboard might redirect to /welcome on first run, or serve the SPA.
    # Either way it's not a 404.
    assert resp.status_code in (200, 302, 307)


# ─── Test 4: OpenAPI docs render ─────────────────────────────────────


async def test_openapi_docs(embedded_client):
    """OpenAPI surface aggregates routes from all mounted apps."""
    resp = await embedded_client.get("/_docs")
    # FastAPI's docs endpoint returns text/html
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()


# ─── Test 5: Lifespan dispatch order is identity → others ────────────


async def test_lifespan_order():
    """Verifies identity boots before any service that depends on JWKS.

    We can't easily inspect order from outside; this test relies on the
    fact that if order were wrong, gateway's create_app() would error
    when trying to fetch JWKS from a not-yet-ready identity. So: a
    successful make_embedded_app() + lifespan-enter is implicit
    confirmation that the order in services list is correct.
    """
    from asgi_lifespan import LifespanManager

    app = make_embedded_app()
    async with LifespanManager(app):
        # If we got here without an exception, lifespan order is fine.
        pass
