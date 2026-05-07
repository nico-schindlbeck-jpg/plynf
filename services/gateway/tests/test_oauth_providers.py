# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Provider-specific OAuth tests for Slack + Linear (v0.4).

These complement ``test_oauth.py`` which covers the GitHub provider end-to-end.
The protocol code is shared, so tests here focus on:

* the provider registry (``get_provider``),
* the per-provider configuration assertions (503 ``OAUTH_NOT_CONFIGURED``),
* the *shape* of the token + userinfo responses for each provider, since
  Slack's token endpoint returns a flat shape and Linear's userinfo is a
  GraphQL POST rather than a REST GET.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.encryption import generate_key
from plinth_gateway.exceptions import OAuthError, OAuthProviderNotConfigured
from plinth_gateway.oauth import (
    GITHUB,
    LINEAR,
    SLACK,
    _parse_token_response,
    assert_provider_configured,
    fetch_user_info,
    get_provider,
    provider_credentials,
    provider_redirect_uri,
)
from plinth_gateway.settings import Settings


# ---------------------------------------------------------------------------
# Settings + client fixtures (per-test — no global pollution).
# ---------------------------------------------------------------------------


@pytest.fixture
def all_providers_settings(tmp_path) -> Settings:
    """Settings with all three OAuth providers configured for tests."""
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        oauth_encryption_key=generate_key(),
        oauth_github_client_id="gh-cid",
        oauth_github_client_secret="gh-cs",
        oauth_github_redirect_uri="http://localhost:7422/v1/oauth/github/callback",
        oauth_slack_client_id="slack-cid",
        oauth_slack_client_secret="slack-cs",
        oauth_slack_redirect_uri="http://localhost:7422/v1/oauth/slack/callback",
        oauth_slack_scopes="channels:read,chat:write,users:read",
        oauth_linear_client_id="linear-cid",
        oauth_linear_client_secret="linear-cs",
        oauth_linear_redirect_uri="http://localhost:7422/v1/oauth/linear/callback",
        oauth_linear_scopes="read,write",
        oauth_state_ttl_seconds=600,
    )


@pytest_asyncio.fixture
async def app_and_client(all_providers_settings: Settings):
    app = create_app(all_providers_settings)
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
# Registry + module-level constants
# ---------------------------------------------------------------------------


def test_slack_provider_defaults() -> None:
    p = get_provider("slack")
    assert p is SLACK
    assert p.name == "slack"
    assert p.authorize_url == "https://slack.com/oauth/v2/authorize"
    assert p.token_url == "https://slack.com/api/oauth.v2.access"
    assert p.userinfo_url == "https://slack.com/api/auth.test"
    assert "channels:read" in p.default_scopes
    # Slack's OAuth v2 flow does not support PKCE.
    assert p.pkce is False


def test_linear_provider_defaults() -> None:
    p = get_provider("linear")
    assert p is LINEAR
    assert p.name == "linear"
    assert p.authorize_url == "https://linear.app/oauth/authorize"
    assert p.token_url == "https://api.linear.app/oauth/token"
    assert p.userinfo_url == "https://api.linear.app/graphql"
    assert "read" in p.default_scopes
    assert "write" in p.default_scopes
    assert p.pkce is True


def test_github_provider_still_works_unchanged() -> None:
    p = get_provider("github")
    assert p is GITHUB
    assert p.pkce is True
    assert "repo" in p.default_scopes


def test_provider_lookup_is_case_insensitive() -> None:
    assert get_provider("SLACK") is SLACK
    assert get_provider("Linear") is LINEAR


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def test_assert_provider_configured_slack(all_providers_settings: Settings) -> None:
    # Should NOT raise — credentials are wired.
    assert_provider_configured(SLACK, all_providers_settings)


def test_assert_provider_configured_linear(all_providers_settings: Settings) -> None:
    assert_provider_configured(LINEAR, all_providers_settings)


def test_assert_provider_not_configured_slack(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_slack_client_id="",
        oauth_slack_client_secret="",
    )
    with pytest.raises(OAuthProviderNotConfigured) as exc:
        assert_provider_configured(SLACK, settings)
    assert "slack" in exc.value.message.lower()
    assert "PLINTH_OAUTH_SLACK_CLIENT_ID" in str(exc.value.details)


def test_assert_provider_not_configured_linear(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_linear_client_id="",
        oauth_linear_client_secret="",
    )
    with pytest.raises(OAuthProviderNotConfigured) as exc:
        assert_provider_configured(LINEAR, settings)
    assert "PLINTH_OAUTH_LINEAR_CLIENT_ID" in str(exc.value.details)


def test_provider_credentials_and_redirect(all_providers_settings: Settings) -> None:
    cid, cs = provider_credentials(SLACK, all_providers_settings)
    assert cid == "slack-cid"
    assert cs == "slack-cs"
    assert (
        provider_redirect_uri(SLACK, all_providers_settings)
        == "http://localhost:7422/v1/oauth/slack/callback"
    )

    cid, cs = provider_credentials(LINEAR, all_providers_settings)
    assert cid == "linear-cid"
    assert cs == "linear-cs"
    assert (
        provider_redirect_uri(LINEAR, all_providers_settings)
        == "http://localhost:7422/v1/oauth/linear/callback"
    )


# ---------------------------------------------------------------------------
# Authorize endpoint — per-provider redirect parameters
# ---------------------------------------------------------------------------


async def test_slack_authorize_redirects_with_correct_params(client) -> None:
    resp = await client.get(
        "/v1/oauth/slack/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://slack.com/oauth/v2/authorize?")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["slack-cid"]
    assert qs["redirect_uri"] == ["http://localhost:7422/v1/oauth/slack/callback"]
    assert qs["scope"] == ["channels:read chat:write users:read"]
    assert qs["response_type"] == ["code"]
    # PKCE is OFF for Slack — challenge should not be set.
    assert "code_challenge" not in qs
    assert "code_challenge_method" not in qs
    # State is server-minted and present.
    assert len(qs["state"][0]) > 16


async def test_linear_authorize_redirects_with_pkce(client) -> None:
    resp = await client.get(
        "/v1/oauth/linear/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://linear.app/oauth/authorize?")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["linear-cid"]
    assert qs["redirect_uri"] == ["http://localhost:7422/v1/oauth/linear/callback"]
    assert qs["scope"] == ["read write"]
    assert qs["response_type"] == ["code"]
    # Linear DOES use PKCE.
    assert qs["code_challenge_method"] == ["S256"]
    assert len(qs["code_challenge"][0]) > 16


async def test_authorize_503_when_slack_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_slack_client_id="",
        oauth_slack_client_secret="",
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
                "/v1/oauth/slack/authorize",
                params={"redirect_uri": "http://x/cb"},
                follow_redirects=False,
            )
            assert resp.status_code == 503
            body = resp.json()
            assert body["error"]["code"] == "OAUTH_NOT_CONFIGURED"
            assert "PLINTH_OAUTH_SLACK_CLIENT_ID" in str(body["error"]["details"])


async def test_authorize_503_when_linear_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_linear_client_id="",
        oauth_linear_client_secret="",
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
                "/v1/oauth/linear/authorize",
                params={"redirect_uri": "http://x/cb"},
                follow_redirects=False,
            )
            assert resp.status_code == 503
            body = resp.json()
            assert body["error"]["code"] == "OAUTH_NOT_CONFIGURED"
            assert "PLINTH_OAUTH_LINEAR_CLIENT_ID" in str(body["error"]["details"])


# ---------------------------------------------------------------------------
# Token-response parsing — per-provider edge cases
# ---------------------------------------------------------------------------


def test_parse_token_response_slack_flat_shape() -> None:
    """Slack's oauth.v2.access response is flat (``ok``, ``access_token``, etc.)."""
    body = {
        "ok": True,
        "access_token": "xoxb-test",
        "token_type": "bot",
        "scope": "channels:read,chat:write",
        "authed_user": {"id": "U123"},
        "team": {"id": "T123", "name": "team"},
        "bot_user_id": "U999",
    }
    grant = _parse_token_response(body)
    assert grant.access_token == "xoxb-test"
    assert grant.token_type == "bot"
    # Comma-separated scopes are normalised to a list.
    assert grant.scopes == ["channels:read", "chat:write"]


def test_parse_token_response_slack_error_shape() -> None:
    body = {"ok": False, "error": "invalid_code"}
    with pytest.raises(OAuthError) as exc:
        _parse_token_response(body)
    assert exc.value.details["provider_error"] == "invalid_code"


def test_parse_token_response_linear_standard_shape() -> None:
    body = {
        "access_token": "lin_oauth_test",
        "token_type": "Bearer",
        "expires_in": 315360000,
        "scope": "read write",
    }
    grant = _parse_token_response(body)
    assert grant.access_token == "lin_oauth_test"
    assert grant.token_type == "Bearer"
    # Space-separated scopes (RFC 6749) — normalised the same way.
    assert grant.scopes == ["read", "write"]
    assert grant.expires_at is not None


# ---------------------------------------------------------------------------
# fetch_user_info — provider-specific shapes
# ---------------------------------------------------------------------------


async def test_fetch_user_info_slack_returns_auth_test_payload() -> None:
    import httpx

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://slack.com/api/auth.test").mock(
                return_value=Response(
                    200,
                    json={
                        "ok": True,
                        "url": "https://team.slack.com/",
                        "team": "team",
                        "user": "alice",
                        "team_id": "T123",
                        "user_id": "U123",
                    },
                )
            )
            info = await fetch_user_info(
                provider=SLACK, access_token="xoxb-test", http_client=http
            )
        assert info["user_id"] == "U123"
        assert info["user"] == "alice"


async def test_fetch_user_info_slack_errors_when_ok_false() -> None:
    import httpx

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://slack.com/api/auth.test").mock(
                return_value=Response(200, json={"ok": False, "error": "invalid_auth"})
            )
            with pytest.raises(OAuthError):
                await fetch_user_info(
                    provider=SLACK, access_token="xoxb-test", http_client=http
                )


async def test_fetch_user_info_linear_uses_graphql_viewer() -> None:
    import httpx

    captured: dict = {}

    def _capture(request):
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "id": "linear-user-1",
                        "name": "Bob",
                        "email": "bob@example.com",
                    }
                }
            },
        )

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://api.linear.app/graphql").mock(side_effect=_capture)
            info = await fetch_user_info(
                provider=LINEAR, access_token="lin_test", http_client=http
            )
        assert info == {
            "id": "linear-user-1",
            "name": "Bob",
            "email": "bob@example.com",
        }
        # Confirm we POSTed a GraphQL viewer query with the bearer token.
        assert captured["auth"] == "Bearer lin_test"
        assert b"viewer" in captured["body"]
        assert b"query" in captured["body"]


async def test_fetch_user_info_linear_propagates_graphql_errors() -> None:
    import httpx

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://api.linear.app/graphql").mock(
                return_value=Response(
                    200,
                    json={
                        "errors": [{"message": "Authentication required"}],
                        "data": None,
                    },
                )
            )
            with pytest.raises(OAuthError) as exc:
                await fetch_user_info(
                    provider=LINEAR, access_token="bad", http_client=http
                )
            assert "Authentication required" in exc.value.message


# ---------------------------------------------------------------------------
# Full callback round-trip — end-to-end happy path
# ---------------------------------------------------------------------------


async def test_slack_callback_creates_connection(app_and_client) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/slack/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://slack.com/api/oauth.v2.access").mock(
            return_value=Response(
                200,
                json={
                    "ok": True,
                    "access_token": "xoxb-realtoken",
                    "token_type": "bot",
                    "scope": "channels:read,chat:write",
                    "authed_user": {"id": "U_AUTH"},
                    "team": {"id": "T_TEAM", "name": "team"},
                    "bot_user_id": "U_BOT",
                },
            )
        )
        mock.get("https://slack.com/api/auth.test").mock(
            return_value=Response(
                200,
                json={
                    "ok": True,
                    "url": "https://team.slack.com/",
                    "team": "team",
                    "user": "alice",
                    "team_id": "T_TEAM",
                    "user_id": "U_AUTH",
                },
            )
        )
        cb = await client.get(
            "/v1/oauth/slack/callback",
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
    assert body["provider"] == "slack"
    assert body["user_id"] == "U_AUTH"
    assert body["user_login"] == "alice"
    assert "channels:read" in body["scopes"]

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "xoxb-realtoken"


async def test_linear_callback_creates_connection(app_and_client) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/linear/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.linear.app/oauth/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "lin_realtoken",
                    "token_type": "Bearer",
                    "expires_in": 315360000,
                    "scope": "read,write",
                },
            )
        )
        mock.post("https://api.linear.app/graphql").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "viewer": {
                            "id": "lin-user-99",
                            "name": "Carol",
                            "email": "carol@example.com",
                        }
                    }
                },
            )
        )
        cb = await client.get(
            "/v1/oauth/linear/callback",
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
    assert body["provider"] == "linear"
    assert body["user_id"] == "lin-user-99"
    assert body["user_login"] == "Carol"
    assert "read" in body["scopes"]

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "lin_realtoken"
