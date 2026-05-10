# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Provider-specific OAuth tests for Google Workspace (v1.1).

These complement ``test_oauth.py`` and ``test_oauth_providers.py`` which
cover the GitHub/Slack/Linear providers. The protocol code is shared, so
tests here focus on:

* the provider registry (``get_provider("google")``),
* the per-provider configuration assertions (503 ``OAUTH_NOT_CONFIGURED``),
* PKCE-ON behavior (Google supports PKCE — verify code_challenge),
* refresh-token round-trip (Google issues refresh tokens),
* full authorize → callback → refresh end-to-end.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.encryption import generate_key
from plinth_gateway.exceptions import OAuthProviderNotConfigured
from plinth_gateway.oauth import (
    GOOGLE,
    assert_provider_configured,
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
def google_settings(tmp_path) -> Settings:
    """Settings with the Google OAuth client configured for tests."""
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        oauth_encryption_key=generate_key(),
        oauth_google_client_id="google-cid",
        oauth_google_client_secret="google-cs",
        oauth_google_redirect_uri="http://localhost:7422/v1/oauth/google/callback",
        oauth_state_ttl_seconds=600,
    )


@pytest_asyncio.fixture
async def app_and_client(google_settings: Settings):
    app = create_app(google_settings)
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
# Registry
# ---------------------------------------------------------------------------


def test_google_provider_defaults() -> None:
    p = get_provider("google")
    assert p is GOOGLE
    assert p.name == "google"
    assert p.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert p.token_url == "https://oauth2.googleapis.com/token"
    assert p.userinfo_url == "https://www.googleapis.com/oauth2/v3/userinfo"
    # Default scopes cover Drive/Docs/Sheets/Calendar/Gmail.
    assert "openid" in p.default_scopes
    assert "https://www.googleapis.com/auth/drive.file" in p.default_scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" in p.default_scopes
    # Google supports PKCE.
    assert p.pkce is True


def test_google_provider_lookup_case_insensitive() -> None:
    assert get_provider("GOOGLE") is GOOGLE
    assert get_provider("Google") is GOOGLE


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def test_assert_google_configured(google_settings: Settings) -> None:
    assert_provider_configured(GOOGLE, google_settings)


def test_assert_google_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_google_client_id="",
        oauth_google_client_secret="",
    )
    with pytest.raises(OAuthProviderNotConfigured) as exc:
        assert_provider_configured(GOOGLE, settings)
    assert "google" in exc.value.message.lower()
    assert "PLINTH_OAUTH_GOOGLE_CLIENT_ID" in str(exc.value.details)


def test_google_provider_credentials_and_redirect(google_settings: Settings) -> None:
    cid, cs = provider_credentials(GOOGLE, google_settings)
    assert cid == "google-cid"
    assert cs == "google-cs"
    assert (
        provider_redirect_uri(GOOGLE, google_settings)
        == "http://localhost:7422/v1/oauth/google/callback"
    )


# ---------------------------------------------------------------------------
# Authorize endpoint — PKCE present
# ---------------------------------------------------------------------------


async def test_google_authorize_redirects_with_pkce(client) -> None:
    resp = await client.get(
        "/v1/oauth/google/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["google-cid"]
    assert qs["redirect_uri"] == ["http://localhost:7422/v1/oauth/google/callback"]
    assert qs["response_type"] == ["code"]
    # Default scopes should include Google's URL scopes.
    scope_str = qs["scope"][0]
    assert "openid" in scope_str
    assert "https://www.googleapis.com/auth/drive.file" in scope_str
    # Google DOES use PKCE.
    assert qs["code_challenge_method"] == ["S256"]
    assert len(qs["code_challenge"][0]) > 16
    assert len(qs["state"][0]) > 16


async def test_google_authorize_503_when_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_google_client_id="",
        oauth_google_client_secret="",
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
                "/v1/oauth/google/authorize",
                params={"redirect_uri": "http://x/cb"},
                follow_redirects=False,
            )
            assert resp.status_code == 503
            body = resp.json()
            assert body["error"]["code"] == "OAUTH_NOT_CONFIGURED"
            assert "PLINTH_OAUTH_GOOGLE_CLIENT_ID" in str(body["error"]["details"])


# ---------------------------------------------------------------------------
# fetch_user_info — Google's standard OIDC userinfo
# ---------------------------------------------------------------------------


async def test_fetch_user_info_google_returns_oidc_payload() -> None:
    import httpx

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://www.googleapis.com/oauth2/v3/userinfo").mock(
                return_value=Response(
                    200,
                    json={
                        "sub": "1234567890",
                        "email": "alice@example.com",
                        "email_verified": True,
                        "name": "Alice Doe",
                        "given_name": "Alice",
                        "family_name": "Doe",
                        "picture": "https://...",
                    },
                )
            )
            info = await fetch_user_info(
                provider=GOOGLE, access_token="ya29.token", http_client=http
            )
        assert info["sub"] == "1234567890"
        assert info["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Full callback round-trip — captures refresh token
# ---------------------------------------------------------------------------


async def test_google_callback_creates_connection_with_refresh(app_and_client) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/google/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "ya29.access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "1//refresh-token",
                    "scope": (
                        "openid email profile "
                        "https://www.googleapis.com/auth/drive.file"
                    ),
                },
            )
        )
        mock.get("https://www.googleapis.com/oauth2/v3/userinfo").mock(
            return_value=Response(
                200,
                json={
                    "sub": "user-google-1",
                    "email": "alice@example.com",
                    "name": "Alice",
                },
            )
        )
        cb = await client.get(
            "/v1/oauth/google/callback",
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
    assert body["provider"] == "google"
    assert body["user_id"] == "user-google-1"
    assert body["user_login"] == "alice@example.com"
    assert body["expires_at"] is not None

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "ya29.access"
    assert decrypted.refresh_token == "1//refresh-token"


# ---------------------------------------------------------------------------
# Refresh path
# ---------------------------------------------------------------------------


async def test_google_refresh_rotates_access_token(app_and_client) -> None:
    app, client = app_and_client

    # Seed a connection that has a refresh token.
    seeded = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "google",
            "user_id": "user-1",
            "user_login": "alice@example.com",
            "scopes": ["openid", "email"],
            "access_token": "ya29.old",
            "refresh_token": "1//refresh-original",
        },
    )
    assert seeded.status_code == 201
    conn_id = seeded.json()["id"]

    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "access_token": "ya29.new",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid email",
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(side_effect=_capture)
        resp = await client.post(
            "/v1/oauth/google/refresh",
            json={"connection_id": conn_id},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["refreshed"] is True
    assert body["expires_at"] is not None

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "ya29.new"
    # The refresh-grant didn't return a new refresh_token, so we keep the original.
    assert decrypted.refresh_token == "1//refresh-original"

    # Confirm we sent a refresh_token grant.
    sent_body = captured["body"].decode("ascii")
    assert "grant_type=refresh_token" in sent_body
    assert "refresh_token=1%2F%2Frefresh-original" in sent_body


async def test_google_refresh_with_rotation_updates_refresh_token(app_and_client) -> None:
    app, client = app_and_client

    seeded = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "google",
            "user_id": "user-2",
            "user_login": "bob@example.com",
            "scopes": ["openid"],
            "access_token": "ya29.old",
            "refresh_token": "1//refresh-original",
        },
    )
    conn_id = seeded.json()["id"]

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "ya29.new",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "1//refresh-rotated",
                    "scope": "openid",
                },
            )
        )
        resp = await client.post(
            "/v1/oauth/google/refresh",
            json={"connection_id": conn_id},
        )
    assert resp.status_code == 200

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "ya29.new"
    # Provider issued a rotated refresh token — it should be persisted.
    assert decrypted.refresh_token == "1//refresh-rotated"


# ---------------------------------------------------------------------------
# Connection list filtering
# ---------------------------------------------------------------------------


async def test_google_connection_list_filters_by_provider(app_and_client) -> None:
    app, client = app_and_client

    a = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "google",
            "user_id": "u-google",
            "user_login": "alice@example.com",
            "scopes": [],
            "access_token": "tok-google",
        },
    )
    assert a.status_code == 201
    b = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "github",
            "user_id": "u-gh",
            "user_login": "bob",
            "scopes": ["repo"],
            "access_token": "tok-gh",
        },
    )
    assert b.status_code == 201

    listed = await client.get("/v1/oauth/connections", params={"provider": "google"})
    assert listed.status_code == 200
    rows = listed.json()["connections"]
    providers = {row["provider"] for row in rows}
    assert providers == {"google"}
