# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end + unit tests for the gateway's OAuth flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.encryption import generate_key
from plinth_gateway.oauth import (
    OAuthConnectionStore,
    OAuthStateStore,
    _new_pkce_pair,
    build_authorize_redirect,
    get_provider,
    parse_scopes,
)
from plinth_gateway.proxy import HttpProxy
from plinth_gateway.settings import Settings


# ---------------------------------------------------------------------------
# Settings + client fixtures (specific to OAuth — separate from the global
# conftest so we can configure the GitHub provider).
# ---------------------------------------------------------------------------


@pytest.fixture
def oauth_settings(tmp_path) -> Settings:
    """Settings with the GitHub OAuth client configured for tests."""
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        oauth_encryption_key=generate_key(),
        oauth_github_client_id="test-client-id",
        oauth_github_client_secret="test-client-secret",
        oauth_github_redirect_uri="http://localhost:7422/v1/oauth/github/callback",
        oauth_github_scopes="repo,read:user",
        oauth_state_ttl_seconds=600,
    )


@pytest_asyncio.fixture
async def app_and_client(oauth_settings: Settings):
    app = create_app(oauth_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        async with app.router.lifespan_context(app):
            yield app, client


@pytest.fixture
def client(app_and_client):
    return app_and_client[1]


# ---------------------------------------------------------------------------
# Provider config + helpers
# ---------------------------------------------------------------------------


def test_github_provider_defaults() -> None:
    p = get_provider("github")
    assert p.name == "github"
    assert "repo" in p.default_scopes
    assert p.pkce is True
    assert p.token_url.endswith("/access_token")


def test_get_provider_unknown_raises() -> None:
    from plinth_gateway.exceptions import OAuthError

    with pytest.raises(OAuthError):
        get_provider("not-a-provider")


def test_pkce_pair_is_s256_compatible() -> None:
    import base64
    import hashlib

    verifier, challenge = _new_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    # Verifier is non-trivial entropy; very minimal smoke check.
    assert len(verifier) >= 43


def test_parse_scopes_supports_comma_and_space() -> None:
    assert parse_scopes("repo,read:user", default=["x"]) == ["repo", "read:user"]
    assert parse_scopes("repo read:user", default=["x"]) == ["repo", "read:user"]
    assert parse_scopes(None, default=["repo"]) == ["repo"]
    assert parse_scopes("", default=["repo"]) == ["repo"]


def test_build_authorize_redirect_includes_pkce_and_state() -> None:
    p = get_provider("github")
    url = build_authorize_redirect(
        p,
        client_id="cid",
        redirect_uri="http://localhost:7422/v1/oauth/github/callback",
        scopes=["repo", "read:user"],
        state="abc",
        pkce_challenge="chal",
    )
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["cid"]
    assert qs["scope"] == ["repo read:user"]
    assert qs["state"] == ["abc"]
    assert qs["code_challenge"] == ["chal"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["response_type"] == ["code"]


def test_build_authorize_redirect_omits_pkce_when_none() -> None:
    p = get_provider("github")
    url = build_authorize_redirect(
        p,
        client_id="cid",
        redirect_uri="http://localhost:7422/cb",
        scopes=["repo"],
        state="abc",
        pkce_challenge=None,
    )
    qs = parse_qs(urlparse(url).query)
    assert "code_challenge" not in qs


# ---------------------------------------------------------------------------
# State store + connection store unit tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_pair(oauth_settings: Settings):
    """Bring up a Database + state/connection stores without the full app."""
    from plinth_gateway.db import Database

    oauth_settings.ensure_data_dir()
    db = Database(oauth_settings.db_path)
    await db.connect()
    state_store = OAuthStateStore(db, ttl_seconds=oauth_settings.oauth_state_ttl_seconds)
    conn_store = OAuthConnectionStore(db, encryption_key=oauth_settings.oauth_encryption_key)
    try:
        yield db, state_store, conn_store
    finally:
        await db.close()


async def test_state_create_and_consume_round_trip(db_pair) -> None:
    _, state_store, _ = db_pair
    state = await state_store.create(
        provider="github",
        redirect_uri="http://app.local/cb",
        scopes=["repo"],
        pkce_verifier="v1",
        tenant_id="default",
    )
    record = await state_store.consume(state, provider="github")
    assert record.redirect_uri == "http://app.local/cb"
    assert record.scopes == ["repo"]
    assert record.pkce_verifier == "v1"


async def test_state_cannot_be_used_twice(db_pair) -> None:
    from plinth_gateway.exceptions import OAuthError

    _, state_store, _ = db_pair
    state = await state_store.create(
        provider="github",
        redirect_uri="http://app.local/cb",
        scopes=["repo"],
        pkce_verifier=None,
    )
    await state_store.consume(state, provider="github")
    with pytest.raises(OAuthError):
        await state_store.consume(state, provider="github")


async def test_state_provider_mismatch_rejected(db_pair) -> None:
    from plinth_gateway.exceptions import OAuthError

    _, state_store, _ = db_pair
    state = await state_store.create(
        provider="github",
        redirect_uri="http://app.local/cb",
        scopes=[],
        pkce_verifier=None,
    )
    with pytest.raises(OAuthError):
        await state_store.consume(state, provider="slack")


async def test_state_unknown_rejected(db_pair) -> None:
    from plinth_gateway.exceptions import OAuthError

    _, state_store, _ = db_pair
    with pytest.raises(OAuthError):
        await state_store.consume("bogus", provider="github")


async def test_connection_create_round_trip(db_pair) -> None:
    _, _, conn_store = db_pair
    public = await conn_store.create(
        tenant_id="default",
        provider="github",
        user_id="42",
        user_login="octocat",
        scopes=["repo"],
        access_token="ghs_test",
        refresh_token="rt_test",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert public.id.startswith("conn_")
    assert public.user_login == "octocat"

    decrypted = await conn_store.get_decrypted(public.id)
    assert decrypted.access_token == "ghs_test"
    assert decrypted.refresh_token == "rt_test"


async def test_connection_list_filters(db_pair) -> None:
    _, _, conn_store = db_pair
    a = await conn_store.create(
        tenant_id="t1", provider="github", user_id="1",
        user_login="a", scopes=[], access_token="t",
    )
    b = await conn_store.create(
        tenant_id="t2", provider="github", user_id="2",
        user_login="b", scopes=[], access_token="t",
    )
    listed = await conn_store.list_public(tenant_id="t1")
    ids = {c.id for c in listed}
    assert a.id in ids
    assert b.id not in ids


async def test_connection_delete(db_pair) -> None:
    from plinth_gateway.exceptions import OAuthConnectionNotFound

    _, _, conn_store = db_pair
    public = await conn_store.create(
        tenant_id="default", provider="github", user_id="1",
        user_login="a", scopes=[], access_token="t",
    )
    await conn_store.delete(public.id)
    with pytest.raises(OAuthConnectionNotFound):
        await conn_store.get_decrypted(public.id)


# ---------------------------------------------------------------------------
# Authorize endpoint
# ---------------------------------------------------------------------------


async def test_authorize_redirects_to_github(client) -> None:
    resp = await client.get(
        "/v1/oauth/github/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["test-client-id"]
    assert qs["redirect_uri"] == ["http://localhost:7422/v1/oauth/github/callback"]
    assert qs["scope"] == ["repo read:user"]
    assert qs["code_challenge_method"] == ["S256"]
    # State and challenge are server-minted and present.
    assert len(qs["state"][0]) > 16
    assert len(qs["code_challenge"][0]) > 16


async def test_authorize_uses_caller_supplied_scopes(client) -> None:
    resp = await client.get(
        "/v1/oauth/github/authorize",
        params={
            "redirect_uri": "http://app.local/done",
            "scopes": "repo,user:email",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["scope"] == ["repo user:email"]


async def test_authorize_503_when_provider_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_github_client_id="",
        oauth_github_client_secret="",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        async with app.router.lifespan_context(app):
            resp = await client.get(
                "/v1/oauth/github/authorize",
                params={"redirect_uri": "http://x/cb"},
                follow_redirects=False,
            )
            assert resp.status_code == 503
            body = resp.json()
            assert body["error"]["code"] == "OAUTH_NOT_CONFIGURED"
            assert "PLINTH_OAUTH_GITHUB_CLIENT_ID" in str(body["error"]["details"])


async def test_unknown_provider_returns_404(client) -> None:
    resp = await client.get(
        "/v1/oauth/notaprovider/authorize",
        params={"redirect_uri": "http://x/cb"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "OAUTH_PROVIDER_UNKNOWN"


# ---------------------------------------------------------------------------
# Callback — full exchange round-trip via respx
# ---------------------------------------------------------------------------


async def test_callback_exchanges_code_and_creates_connection(
    app_and_client,
) -> None:
    app, client = app_and_client

    # 1) Hit /authorize to mint a real state + verifier.
    resp = await client.get(
        "/v1/oauth/github/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    # 2) Mock GitHub's token + user endpoints.
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "ghs_realtoken",
                    "refresh_token": "ghr_realrefresh",
                    "expires_in": 3600,
                    "scope": "repo,read:user",
                    "token_type": "bearer",
                },
            )
        )
        mock.get("https://api.github.com/user").mock(
            return_value=Response(
                200,
                json={"id": 12345, "login": "octocat", "name": "Mona"},
            )
        )

        cb = await client.get(
            "/v1/oauth/github/callback",
            params={"code": "auth-code-xyz", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 302
        target = cb.headers["location"]
        assert target.startswith("http://app.local/done?")
        qs = parse_qs(urlparse(target).query)
        conn_id = qs["connection_id"][0]
        assert conn_id.startswith("conn_")

    # 3) The connection should now be queryable + the token decryptable.
    public = await client.get(f"/v1/oauth/connections/{conn_id}")
    assert public.status_code == 200
    body = public.json()
    assert body["provider"] == "github"
    assert body["user_id"] == "12345"
    assert body["user_login"] == "octocat"
    assert "repo" in body["scopes"]
    # Tokens are NEVER in the public view.
    assert "access_token" not in body
    assert "refresh_token" not in body

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "ghs_realtoken"
    assert decrypted.refresh_token == "ghr_realrefresh"


async def test_callback_state_replay_rejected(app_and_client) -> None:
    _, client = app_and_client

    resp = await client.get(
        "/v1/oauth/github/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(200, json={"access_token": "t", "scope": "repo"})
        )
        mock.get("https://api.github.com/user").mock(
            return_value=Response(200, json={"id": 1, "login": "u"})
        )

        # First use OK.
        cb = await client.get(
            "/v1/oauth/github/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 302

        # Second use blocked.
        cb2 = await client.get(
            "/v1/oauth/github/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert cb2.status_code == 400
        assert cb2.json()["error"]["code"] == "OAUTH_ERROR"


async def test_callback_provider_error_propagates(client) -> None:
    resp = await client.get(
        "/v1/oauth/github/callback",
        params={"error": "access_denied", "error_description": "user said no"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "OAUTH_ERROR"
    assert body["error"]["details"]["provider_error"] == "access_denied"


async def test_callback_token_exchange_error_propagates(app_and_client) -> None:
    _, client = app_and_client
    resp = await client.get(
        "/v1/oauth/github/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(
                200,
                json={"error": "bad_verification_code"},
            )
        )
        cb = await client.get(
            "/v1/oauth/github/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 400
        assert cb.json()["error"]["details"]["provider_error"] == "bad_verification_code"


async def test_callback_form_encoded_response_parsed(app_and_client) -> None:
    """GitHub's default response is form-encoded; we tolerate it."""
    _, client = app_and_client
    resp = await client.get(
        "/v1/oauth/github/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(
                200,
                content=b"access_token=ghs_form&token_type=bearer&scope=repo",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        )
        mock.get("https://api.github.com/user").mock(
            return_value=Response(200, json={"id": 1, "login": "u"})
        )
        cb = await client.get(
            "/v1/oauth/github/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 302


# ---------------------------------------------------------------------------
# Connections CRUD via API
# ---------------------------------------------------------------------------


async def test_create_and_list_connections(client) -> None:
    body = {
        "provider": "github",
        "user_id": "1",
        "user_login": "octocat",
        "scopes": ["repo"],
        "access_token": "tok",
        "refresh_token": "rt",
        "tenant_id": "tenant-a",
    }
    r = await client.post("/v1/oauth/connections", json=body)
    assert r.status_code == 201
    public = r.json()
    assert public["id"].startswith("conn_")
    assert "access_token" not in public

    listed = await client.get("/v1/oauth/connections", params={"tenant_id": "tenant-a"})
    assert listed.status_code == 200
    items = listed.json()["connections"]
    assert len(items) == 1
    assert items[0]["id"] == public["id"]


async def test_delete_connection(client) -> None:
    r = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "github",
            "user_id": "1",
            "user_login": "u",
            "scopes": [],
            "access_token": "t",
        },
    )
    cid = r.json()["id"]

    d = await client.delete(f"/v1/oauth/connections/{cid}")
    assert d.status_code == 204

    g = await client.get(f"/v1/oauth/connections/{cid}")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "OAUTH_CONNECTION_NOT_FOUND"


async def test_get_unknown_connection_404(client) -> None:
    r = await client.get("/v1/oauth/connections/conn_does_not_exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


async def test_refresh_endpoint_success(app_and_client) -> None:
    app, client = app_and_client
    # Seed a connection with a refresh token.
    public = await app.state.oauth_connections.create(
        tenant_id="default",
        provider="github",
        user_id="1",
        user_login="u",
        scopes=["repo"],
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "scope": "repo",
                },
            )
        )
        r = await client.post(
            "/v1/oauth/github/refresh",
            json={"connection_id": public.id},
        )
        assert r.status_code == 200
        assert r.json()["refreshed"] is True

    decrypted = await app.state.oauth_connections.get_decrypted(public.id)
    assert decrypted.access_token == "new-access"
    assert decrypted.refresh_token == "new-refresh"


async def test_refresh_without_token_rejected(app_and_client) -> None:
    app, client = app_and_client
    public = await app.state.oauth_connections.create(
        tenant_id="default",
        provider="github",
        user_id="1",
        user_login="u",
        scopes=[],
        access_token="x",
        refresh_token=None,
    )
    r = await client.post(
        "/v1/oauth/github/refresh",
        json={"connection_id": public.id},
    )
    assert r.status_code == 400
    assert "refresh" in r.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Proxy attaches OAuth bearer for tools with auth_method=oauth2
# ---------------------------------------------------------------------------


async def test_proxy_attaches_oauth_bearer_via_connection(db_pair) -> None:
    """Direct proxy test: bearer header should come from the decrypted token."""
    from plinth_gateway.models import Tool

    _, _, conn_store = db_pair
    public = await conn_store.create(
        tenant_id="default",
        provider="github",
        user_id="1",
        user_login="u",
        scopes=["repo"],
        access_token="ghs_via_proxy",
    )
    tool = Tool.model_validate(
        {
            "tool_id": "github.list_issues",
            "name": "list issues",
            "description": "list",
            "transport": "http",
            "endpoint": "http://mcp.test/invoke/github.list_issues",
            "input_schema": {},
            "output_schema": {},
            "idempotent": True,
            "side_effects": "read",
            "cache_ttl_seconds": 60,
            "auth_method": "oauth2",
            "auth_config": {"provider": "github", "connection_id": public.id},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/github.list_issues").mock(
                side_effect=_capture
            )
            await proxy.invoke(tool, {"repo": "o/r"}, connection_store=conn_store)
            assert captured["auth"] == "Bearer ghs_via_proxy"
    finally:
        await proxy.aclose()


async def test_proxy_oauth2_no_connection_id_falls_back_to_mock(db_pair) -> None:
    """If a tool has no connection_id wired, we keep the v0.1 mock_token path."""
    from plinth_gateway.models import Tool

    _, _, conn_store = db_pair
    tool = Tool.model_validate(
        {
            "tool_id": "x.tool",
            "name": "x",
            "description": "x",
            "transport": "http",
            "endpoint": "http://mcp.test/invoke/x",
            "input_schema": {},
            "output_schema": {},
            "idempotent": True,
            "side_effects": "read",
            "cache_ttl_seconds": 60,
            "auth_method": "oauth2",
            "auth_config": {"mock_token": "legacy"},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/x").mock(side_effect=_capture)
            await proxy.invoke(tool, {}, connection_store=conn_store)
            assert captured["auth"] == "Bearer legacy"
    finally:
        await proxy.aclose()


async def test_proxy_oauth2_refreshes_expired_token(
    oauth_settings, db_pair
) -> None:
    """Expired tokens with a refresh token are silently refreshed."""
    from plinth_gateway.models import Tool

    _, _, conn_store = db_pair
    public = await conn_store.create(
        tenant_id="default",
        provider="github",
        user_id="1",
        user_login="u",
        scopes=["repo"],
        access_token="expired-token",
        refresh_token="rt",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    tool = Tool.model_validate(
        {
            "tool_id": "github.x",
            "name": "x",
            "description": "x",
            "transport": "http",
            "endpoint": "http://mcp.test/invoke/x",
            "input_schema": {},
            "output_schema": {},
            "idempotent": True,
            "side_effects": "read",
            "cache_ttl_seconds": 60,
            "auth_method": "oauth2",
            "auth_config": {"provider": "github", "connection_id": public.id},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://github.com/login/oauth/access_token").mock(
                return_value=Response(
                    200,
                    json={
                        "access_token": "fresh-token",
                        "refresh_token": "rt2",
                        "expires_in": 3600,
                        "scope": "repo",
                    },
                )
            )
            mock.post("http://mcp.test/invoke/x").mock(side_effect=_capture)
            await proxy.invoke(
                tool,
                {},
                connection_store=conn_store,
                settings=oauth_settings,
            )
            assert captured["auth"] == "Bearer fresh-token"
    finally:
        await proxy.aclose()


async def test_proxy_oauth2_refresh_failure_falls_back(oauth_settings, db_pair) -> None:
    """If refresh fails the proxy falls back to the cached token."""
    from plinth_gateway.models import Tool

    _, _, conn_store = db_pair
    public = await conn_store.create(
        tenant_id="default",
        provider="github",
        user_id="1",
        user_login="u",
        scopes=["repo"],
        access_token="stale-token",
        refresh_token="rt",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    tool = Tool.model_validate(
        {
            "tool_id": "github.x",
            "name": "x",
            "description": "x",
            "transport": "http",
            "endpoint": "http://mcp.test/invoke/x",
            "input_schema": {},
            "output_schema": {},
            "idempotent": True,
            "side_effects": "read",
            "cache_ttl_seconds": 60,
            "auth_method": "oauth2",
            "auth_config": {"provider": "github", "connection_id": public.id},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://github.com/login/oauth/access_token").mock(
                return_value=Response(500, text="boom")
            )
            mock.post("http://mcp.test/invoke/x").mock(side_effect=_capture)
            await proxy.invoke(
                tool,
                {},
                connection_store=conn_store,
                settings=oauth_settings,
            )
            # Refresh failed → we used the stale token rather than crashing.
            assert captured["auth"] == "Bearer stale-token"
    finally:
        await proxy.aclose()
