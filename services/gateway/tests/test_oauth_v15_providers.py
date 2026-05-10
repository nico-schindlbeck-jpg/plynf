# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Provider-specific OAuth tests for the v1.5 additions:

* Atlassian (Jira + Confluence) — cloudid metadata flow
* Salesforce — instance_url metadata flow
* Asana — vanilla bearer; no extra metadata

These are dedicated to exercising the new branches in
``oauth.py`` (registry + cloudid fetch helper) and the corresponding
metadata storage path in ``oauth_api.py`` callbacks.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.encryption import generate_key
from plinth_gateway.exceptions import OAuthError, OAuthProviderNotConfigured
from plinth_gateway.oauth import (
    ASANA,
    ATLASSIAN,
    SALESFORCE,
    assert_provider_configured,
    fetch_atlassian_cloudid,
    fetch_user_info,
    get_provider,
    provider_credentials,
    provider_redirect_uri,
)
from plinth_gateway.settings import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def v15_settings(tmp_path) -> Settings:
    """Settings with all three new OAuth clients configured for tests."""
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        oauth_encryption_key=generate_key(),
        oauth_atlassian_client_id="atl-cid",
        oauth_atlassian_client_secret="atl-cs",
        oauth_atlassian_redirect_uri="http://localhost:7422/v1/oauth/atlassian/callback",
        oauth_salesforce_client_id="sf-cid",
        oauth_salesforce_client_secret="sf-cs",
        oauth_salesforce_redirect_uri="http://localhost:7422/v1/oauth/salesforce/callback",
        oauth_asana_client_id="asana-cid",
        oauth_asana_client_secret="asana-cs",
        oauth_asana_redirect_uri="http://localhost:7422/v1/oauth/asana/callback",
        oauth_state_ttl_seconds=600,
    )


@pytest_asyncio.fixture
async def app_and_client(v15_settings: Settings):
    app = create_app(v15_settings)
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
# Registry — Atlassian
# ---------------------------------------------------------------------------


def test_atlassian_provider_defaults() -> None:
    p = get_provider("atlassian")
    assert p is ATLASSIAN
    assert p.name == "atlassian"
    assert p.authorize_url == "https://auth.atlassian.com/authorize"
    assert p.token_url == "https://auth.atlassian.com/oauth/token"
    assert p.userinfo_url == "https://api.atlassian.com/me"
    assert "read:jira-work" in p.default_scopes
    assert "write:confluence-content" in p.default_scopes
    assert "offline_access" in p.default_scopes
    assert p.pkce is True
    # Atlassian's authorize URL requires the audience parameter.
    assert p.extra_authorize_params is not None
    assert p.extra_authorize_params.get("audience") == "api.atlassian.com"


def test_atlassian_provider_lookup_case_insensitive() -> None:
    assert get_provider("ATLASSIAN") is ATLASSIAN


def test_atlassian_assert_configured(v15_settings: Settings) -> None:
    assert_provider_configured(ATLASSIAN, v15_settings)


def test_atlassian_assert_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_atlassian_client_id="",
        oauth_atlassian_client_secret="",
    )
    with pytest.raises(OAuthProviderNotConfigured) as exc:
        assert_provider_configured(ATLASSIAN, settings)
    assert "atlassian" in exc.value.message.lower()
    assert "PLINTH_OAUTH_ATLASSIAN_CLIENT_ID" in str(exc.value.details)


# ---------------------------------------------------------------------------
# Registry — Salesforce
# ---------------------------------------------------------------------------


def test_salesforce_provider_defaults() -> None:
    p = get_provider("salesforce")
    assert p is SALESFORCE
    assert p.authorize_url.startswith("https://login.salesforce.com/")
    assert "api" in p.default_scopes
    assert "refresh_token" in p.default_scopes
    assert p.pkce is True


def test_salesforce_assert_configured(v15_settings: Settings) -> None:
    assert_provider_configured(SALESFORCE, v15_settings)


def test_salesforce_assert_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_salesforce_client_id="",
        oauth_salesforce_client_secret="",
    )
    with pytest.raises(OAuthProviderNotConfigured):
        assert_provider_configured(SALESFORCE, settings)


# ---------------------------------------------------------------------------
# Registry — Asana
# ---------------------------------------------------------------------------


def test_asana_provider_defaults() -> None:
    p = get_provider("asana")
    assert p is ASANA
    assert p.authorize_url == "https://app.asana.com/-/oauth_authorize"
    assert p.token_url == "https://app.asana.com/-/oauth_token"
    assert p.userinfo_url == "https://app.asana.com/api/1.0/users/me"
    assert p.default_scopes == ["default"]
    assert p.pkce is True


def test_asana_assert_configured(v15_settings: Settings) -> None:
    assert_provider_configured(ASANA, v15_settings)


def test_asana_provider_credentials_and_redirect(v15_settings: Settings) -> None:
    cid, cs = provider_credentials(ASANA, v15_settings)
    assert cid == "asana-cid"
    assert cs == "asana-cs"
    assert (
        provider_redirect_uri(ASANA, v15_settings)
        == "http://localhost:7422/v1/oauth/asana/callback"
    )


# ---------------------------------------------------------------------------
# Authorize — Atlassian carries audience param
# ---------------------------------------------------------------------------


async def test_atlassian_authorize_includes_audience(client) -> None:
    resp = await client.get(
        "/v1/oauth/atlassian/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://auth.atlassian.com/authorize?")
    qs = parse_qs(urlparse(location).query)
    # PKCE on for Atlassian.
    assert "code_challenge" in qs
    assert qs["code_challenge_method"] == ["S256"]
    # Audience is the new bit — Atlassian rejects without it.
    assert qs["audience"] == ["api.atlassian.com"]


async def test_salesforce_authorize_pkce_on(client) -> None:
    resp = await client.get(
        "/v1/oauth/salesforce/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert "code_challenge" in qs


async def test_asana_authorize_pkce_on(client) -> None:
    resp = await client.get(
        "/v1/oauth/asana/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://app.asana.com/-/oauth_authorize?")
    qs = parse_qs(urlparse(location).query)
    assert "code_challenge" in qs


# ---------------------------------------------------------------------------
# fetch_atlassian_cloudid helper
# ---------------------------------------------------------------------------


async def test_fetch_atlassian_cloudid_returns_first() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get(
                "https://api.atlassian.com/oauth/token/accessible-resources"
            ).mock(
                return_value=Response(
                    200,
                    json=[
                        {"id": "cloud-1", "name": "Acme", "scopes": []},
                        {"id": "cloud-2", "name": "Beta", "scopes": []},
                    ],
                )
            )
            cid = await fetch_atlassian_cloudid(
                access_token="t", http_client=http
            )
        assert cid == "cloud-1"


async def test_fetch_atlassian_cloudid_empty_returns_none() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get(
                "https://api.atlassian.com/oauth/token/accessible-resources"
            ).mock(return_value=Response(200, json=[]))
            cid = await fetch_atlassian_cloudid(
                access_token="t", http_client=http
            )
        assert cid is None


async def test_fetch_atlassian_cloudid_4xx_raises() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get(
                "https://api.atlassian.com/oauth/token/accessible-resources"
            ).mock(return_value=Response(401, json={"error": "unauthorized"}))
            with pytest.raises(OAuthError):
                await fetch_atlassian_cloudid(access_token="t", http_client=http)


# ---------------------------------------------------------------------------
# Userinfo — Asana wraps under "data"
# ---------------------------------------------------------------------------


async def test_fetch_user_info_asana_unwraps_data() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://app.asana.com/api/1.0/users/me").mock(
                return_value=Response(
                    200,
                    json={
                        "data": {
                            "gid": "user-1",
                            "name": "Alice",
                            "email": "alice@example.com",
                        }
                    },
                )
            )
            info = await fetch_user_info(
                provider=ASANA, access_token="t", http_client=http
            )
        # Unwrapped — flat dict with id_field/login_field at the top level.
        assert info["gid"] == "user-1"
        assert info["name"] == "Alice"


async def test_fetch_user_info_atlassian() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://api.atlassian.com/me").mock(
                return_value=Response(
                    200,
                    json={
                        "account_id": "acc-1",
                        "email": "alice@example.com",
                        "name": "Alice",
                    },
                )
            )
            info = await fetch_user_info(
                provider=ATLASSIAN, access_token="t", http_client=http
            )
        assert info["account_id"] == "acc-1"
        assert info["email"] == "alice@example.com"


async def test_fetch_user_info_salesforce() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get(
                "https://login.salesforce.com/services/oauth2/userinfo"
            ).mock(
                return_value=Response(
                    200,
                    json={
                        "user_id": "005xx0000012345",
                        "preferred_username": "alice@acme.com",
                        "email": "alice@acme.com",
                    },
                )
            )
            info = await fetch_user_info(
                provider=SALESFORCE, access_token="t", http_client=http
            )
        assert info["user_id"] == "005xx0000012345"


# ---------------------------------------------------------------------------
# Full callback round-trips with metadata flow
# ---------------------------------------------------------------------------


async def test_atlassian_callback_stores_cloudid_metadata(app_and_client) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/atlassian/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://auth.atlassian.com/oauth/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "atl-access",
                    "refresh_token": "atl-refresh",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "scope": "read:jira-work",
                },
            )
        )
        mock.get("https://api.atlassian.com/me").mock(
            return_value=Response(
                200,
                json={
                    "account_id": "acc-1",
                    "email": "alice@example.com",
                    "name": "Alice",
                },
            )
        )
        mock.get(
            "https://api.atlassian.com/oauth/token/accessible-resources"
        ).mock(
            return_value=Response(
                200,
                json=[
                    {"id": "cloud-abc-123", "name": "Acme", "scopes": []},
                ],
            )
        )
        cb = await client.get(
            "/v1/oauth/atlassian/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert cb.status_code == 302
    target = cb.headers["location"]
    qs = parse_qs(urlparse(target).query)
    conn_id = qs["connection_id"][0]

    # Verify the connection has cloudid in its metadata.
    public = await client.get(f"/v1/oauth/connections/{conn_id}")
    assert public.status_code == 200
    body = public.json()
    assert body["provider"] == "atlassian"
    assert body["user_id"] == "acc-1"
    assert body["metadata"]["cloudid"] == "cloud-abc-123"


async def test_salesforce_callback_stores_instance_url_metadata(
    app_and_client,
) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/salesforce/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        # Salesforce's token endpoint returns instance_url in the body itself.
        mock.post(
            "https://login.salesforce.com/services/oauth2/token"
        ).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "sf-access",
                    "refresh_token": "sf-refresh",
                    "token_type": "bearer",
                    "instance_url": "https://acme.my.salesforce.com",
                    "scope": "api refresh_token",
                },
            )
        )
        mock.get(
            "https://login.salesforce.com/services/oauth2/userinfo"
        ).mock(
            return_value=Response(
                200,
                json={
                    "user_id": "005xx0000012345",
                    "preferred_username": "alice@acme.com",
                    "email": "alice@acme.com",
                },
            )
        )
        cb = await client.get(
            "/v1/oauth/salesforce/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert cb.status_code == 302
    target = cb.headers["location"]
    qs = parse_qs(urlparse(target).query)
    conn_id = qs["connection_id"][0]

    public = await client.get(f"/v1/oauth/connections/{conn_id}")
    assert public.status_code == 200
    body = public.json()
    assert body["provider"] == "salesforce"
    assert body["metadata"]["instance_url"] == "https://acme.my.salesforce.com"


async def test_asana_callback_no_metadata(app_and_client) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/asana/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://app.asana.com/-/oauth_token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "asana-access",
                    "refresh_token": "asana-refresh",
                    "token_type": "bearer",
                    "data": {"id": "user-1", "name": "Alice"},
                },
            )
        )
        mock.get("https://app.asana.com/api/1.0/users/me").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "gid": "user-1",
                        "name": "Alice",
                        "email": "alice@example.com",
                    }
                },
            )
        )
        cb = await client.get(
            "/v1/oauth/asana/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert cb.status_code == 302
    qs = parse_qs(urlparse(cb.headers["location"]).query)
    conn_id = qs["connection_id"][0]

    public = await client.get(f"/v1/oauth/connections/{conn_id}")
    assert public.status_code == 200
    body = public.json()
    assert body["provider"] == "asana"
    assert body["user_id"] == "user-1"
    # Asana exposes no extra metadata.
    assert body["metadata"] == {}


# ---------------------------------------------------------------------------
# Manual create with metadata
# ---------------------------------------------------------------------------


async def test_create_connection_persists_metadata(client) -> None:
    resp = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "atlassian",
            "user_id": "acc-1",
            "user_login": "alice",
            "scopes": ["read:jira-work"],
            "access_token": "tok",
            "metadata": {"cloudid": "cloud-xyz"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    conn_id = body["id"]
    assert body["metadata"]["cloudid"] == "cloud-xyz"

    # Round-trip via GET.
    fetched = await client.get(f"/v1/oauth/connections/{conn_id}")
    assert fetched.status_code == 200
    assert fetched.json()["metadata"]["cloudid"] == "cloud-xyz"


# ---------------------------------------------------------------------------
# Proxy injects metadata headers — verify via the OAuthConnection store path.
# ---------------------------------------------------------------------------


async def test_proxy_metadata_headers_for_atlassian(app_and_client) -> None:
    app, client = app_and_client

    # Seed an Atlassian connection with cloudid metadata.
    create = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "atlassian",
            "user_id": "acc-1",
            "user_login": "alice",
            "access_token": "atl-bearer",
            "scopes": [],
            "metadata": {"cloudid": "cloud-zzz"},
        },
    )
    assert create.status_code == 201
    conn_id = create.json()["id"]

    # Decrypt + check the metadata flowed through the store.
    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.metadata == {"cloudid": "cloud-zzz"}
    assert decrypted.access_token == "atl-bearer"

    # Verify proxy._metadata_headers builds the correct header set.
    from plinth_gateway.proxy import _metadata_headers

    headers = _metadata_headers(decrypted)
    assert headers == {"X-Plinth-OAuth-Cloudid": "cloud-zzz"}


async def test_proxy_metadata_headers_for_salesforce(app_and_client) -> None:
    app, client = app_and_client

    create = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "salesforce",
            "user_id": "005xx0000012345",
            "user_login": "alice@acme.com",
            "access_token": "sf-bearer",
            "scopes": [],
            "metadata": {"instance_url": "https://acme.my.salesforce.com"},
        },
    )
    assert create.status_code == 201
    conn_id = create.json()["id"]

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.metadata == {"instance_url": "https://acme.my.salesforce.com"}

    from plinth_gateway.proxy import _metadata_headers

    headers = _metadata_headers(decrypted)
    assert headers == {
        "X-Plinth-OAuth-InstanceUrl": "https://acme.my.salesforce.com"
    }


async def test_proxy_metadata_headers_empty_for_asana(app_and_client) -> None:
    app, client = app_and_client

    create = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "asana",
            "user_id": "user-1",
            "access_token": "asana-bearer",
            "scopes": [],
        },
    )
    assert create.status_code == 201
    conn_id = create.json()["id"]

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    from plinth_gateway.proxy import _metadata_headers

    assert _metadata_headers(decrypted) == {}
