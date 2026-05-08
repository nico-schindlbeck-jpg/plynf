# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the SDK's :class:`IdentityClient` end-to-end (against respx)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from plinth import (
    IdentityClient,
    InvalidToken,
    Plinth,
    TokenExpired,
    TokenRevoked,
)

UTC = timezone.utc

IDENTITY_URL = "http://identity.test"


def _claims(
    *,
    jti: str = "jti_test",
    agent_id: str = "agt_1",
    tenant_id: str = "default",
    scopes: list[str] | None = None,
) -> dict:
    now = int(datetime.now(UTC).timestamp())
    return {
        "sub": agent_id,
        "iss": IDENTITY_URL,
        "aud": "plinth",
        "iat": now,
        "exp": now + 3600,
        "jti": jti,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "workspace_id": None,
        "scopes": scopes or [],
        "rate_limit": None,
    }


def _identity_client(router: respx.MockRouter) -> IdentityClient:
    return IdentityClient(
        IDENTITY_URL,
        api_key="test-key",
        transport=httpx.MockTransport(router.handler),
    )


@pytest.fixture
def identity_mock() -> respx.MockRouter:
    with respx.mock(base_url=IDENTITY_URL, assert_all_called=False) as router:
        yield router


def test_issue_token_round_trips(identity_mock: respx.MockRouter):
    expected_claims = _claims(jti="jti_abc", tenant_id="acme", scopes=["s1"])
    identity_mock.post("/v1/tokens").respond(
        201,
        json={
            "token": "fake.jwt.token",
            "jti": "jti_abc",
            "expires_at": datetime.now(UTC).isoformat(),
            "claims": expected_claims,
        },
    )
    client = _identity_client(identity_mock)
    response = client.issue_token(
        "agt_1",
        scopes=["s1"],
        tenant_id="acme",
        ttl_seconds=600,
    )
    assert response.token == "fake.jwt.token"
    assert response.jti == "jti_abc"
    assert response.claims.tenant_id == "acme"
    assert response.claims.scopes == ["s1"]


def test_verify_token_returns_claims(identity_mock: respx.MockRouter):
    expected = _claims(jti="jti_v")
    identity_mock.post("/v1/tokens/verify").respond(200, json=expected)
    client = _identity_client(identity_mock)
    claims = client.verify_token("any-token")
    assert claims.jti == "jti_v"


def test_verify_expired_raises_token_expired(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/tokens/verify").respond(
        401,
        json={"error": {"code": "TOKEN_EXPIRED", "message": "expired", "details": {}}},
    )
    client = _identity_client(identity_mock)
    with pytest.raises(TokenExpired):
        client.verify_token("any")


def test_verify_revoked_raises_token_revoked(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/tokens/verify").respond(
        401,
        json={"error": {"code": "TOKEN_REVOKED", "message": "revoked", "details": {}}},
    )
    client = _identity_client(identity_mock)
    with pytest.raises(TokenRevoked):
        client.verify_token("any")


def test_verify_invalid_raises_invalid_token(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/tokens/verify").respond(
        401,
        json={"error": {"code": "INVALID_TOKEN", "message": "tampered", "details": {}}},
    )
    client = _identity_client(identity_mock)
    with pytest.raises(InvalidToken):
        client.verify_token("any")


def test_revoke_token_returns_none(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/tokens/jti_xyz/revoke").respond(204)
    client = _identity_client(identity_mock)
    assert client.revoke_token("jti_xyz") is None


def test_get_token_info_does_not_include_secret(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tokens/jti_info").respond(
        200,
        json={
            "jti": "jti_info",
            "agent_id": "agt_1",
            "tenant_id": "acme",
            "workspace_id": None,
            "scopes": ["a"],
            "issued_at": datetime.now(UTC).isoformat(),
            "expires_at": datetime.now(UTC).isoformat(),
            "revoked": False,
            "revoked_at": None,
            "metadata": {"created_by": "tests"},
        },
    )
    client = _identity_client(identity_mock)
    info = client.get_token_info("jti_info")
    assert info.jti == "jti_info"
    assert info.agent_id == "agt_1"
    assert info.tenant_id == "acme"
    assert info.metadata == {"created_by": "tests"}


def test_plinth_facade_exposes_identity_when_url_passed(
    identity_mock: respx.MockRouter,
    workspace_mock,
    gateway_mock,
):
    identity_mock.post("/v1/tokens").respond(
        201,
        json={
            "token": "tk",
            "jti": "jti_facade",
            "expires_at": datetime.now(UTC).isoformat(),
            "claims": _claims(jti="jti_facade"),
        },
    )
    plinth = Plinth(
        workspace_url="http://workspace.test",
        gateway_url="http://gateway.test",
        identity_url=IDENTITY_URL,
        api_key="test-key",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
        identity_transport=httpx.MockTransport(identity_mock.handler),
    )
    assert plinth.identity is not None
    response = plinth.identity.issue_token("agt_1", ["read"])
    assert response.jti == "jti_facade"
    plinth.close()


def test_plinth_facade_identity_is_none_without_url(workspace_mock, gateway_mock):
    plinth = Plinth(
        workspace_url="http://workspace.test",
        gateway_url="http://gateway.test",
        api_key="test-key",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
    )
    assert plinth.identity is None
    plinth.close()


def test_round_trip_issue_verify_revoke(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/tokens").respond(
        201,
        json={
            "token": "rt-token",
            "jti": "jti_rt",
            "expires_at": datetime.now(UTC).isoformat(),
            "claims": _claims(jti="jti_rt"),
        },
    )
    identity_mock.post("/v1/tokens/verify").respond(200, json=_claims(jti="jti_rt"))
    identity_mock.post("/v1/tokens/jti_rt/revoke").respond(204)

    client = _identity_client(identity_mock)
    issued = client.issue_token("agt_rt", scopes=["x"])
    assert issued.jti == "jti_rt"
    claims = client.verify_token(issued.token)
    assert claims.jti == "jti_rt"
    client.revoke_token("jti_rt")


def test_workspace_model_carries_tenant_id():
    """SDK Workspace model now exposes ``tenant_id``."""

    from plinth.models import Workspace

    ws = Workspace(
        id="ws_x",
        name="n",
        tenant_id="acme",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert ws.tenant_id == "acme"


def test_workspace_model_defaults_tenant_id_to_default():
    from plinth.models import Workspace

    ws = Workspace(
        id="ws_x",
        name="n",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert ws.tenant_id == "default"


def test_list_tokens_round_trips(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tokens").respond(
        200,
        json={
            "tokens": [
                {
                    "jti": "jti_1",
                    "agent_id": "agt_1",
                    "tenant_id": "default",
                    "workspace_id": None,
                    "scopes": [],
                    "issued_at": datetime.now(UTC).isoformat(),
                    "expires_at": datetime.now(UTC).isoformat(),
                    "revoked": False,
                    "revoked_at": None,
                    "metadata": {},
                },
            ],
        },
    )
    client = _identity_client(identity_mock)
    tokens = client.list_tokens(revoked=False)
    assert len(tokens) == 1
    assert tokens[0].jti == "jti_1"


def test_list_tokens_revoked_only(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tokens").respond(
        200,
        json={
            "tokens": [
                {
                    "jti": "jti_revoked",
                    "agent_id": "a",
                    "tenant_id": "default",
                    "workspace_id": None,
                    "scopes": [],
                    "issued_at": datetime.now(UTC).isoformat(),
                    "expires_at": datetime.now(UTC).isoformat(),
                    "revoked": True,
                    "revoked_at": datetime.now(UTC).isoformat(),
                    "metadata": {},
                },
            ],
        },
    )
    client = _identity_client(identity_mock)
    tokens = client.list_tokens(revoked=True, since=datetime.now(UTC))
    assert tokens[0].revoked is True


def test_list_tenants_returns_default(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tenants").respond(
        200,
        json={
            "tenants": [
                {
                    "id": "default",
                    "name": "Default",
                    "metadata": {},
                    "created_at": datetime.now(UTC).isoformat(),
                },
                {
                    "id": "acme",
                    "name": "Acme",
                    "metadata": {},
                    "created_at": datetime.now(UTC).isoformat(),
                },
            ],
        },
    )
    client = _identity_client(identity_mock)
    tenants = client.list_tenants()
    ids = {t["id"] for t in tenants}
    assert {"default", "acme"} <= ids


def test_create_tenant(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/tenants").respond(
        201,
        json={
            "id": "newco",
            "name": "NewCo",
            "metadata": {"plan": "starter"},
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    client = _identity_client(identity_mock)
    tenant = client.create_tenant("newco", "NewCo", metadata={"plan": "starter"})
    assert tenant["id"] == "newco"


def test_get_tenant(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tenants/acme").respond(
        200,
        json={
            "id": "acme",
            "name": "Acme",
            "metadata": {},
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    client = _identity_client(identity_mock)
    tenant = client.get_tenant("acme")
    assert tenant["id"] == "acme"


def test_plinth_facade_tenants_list(workspace_mock, gateway_mock):
    """``Plinth.tenants_list()`` calls the workspace's ``/v1/tenants`` endpoint."""

    workspace_mock.get("/v1/tenants").respond(
        200,
        json={
            "tenants": [
                {"id": "default", "workspace_count": 2},
                {"id": "acme", "workspace_count": 1},
            ],
        },
    )
    plinth = Plinth(
        workspace_url="http://workspace.test",
        gateway_url="http://gateway.test",
        api_key="test-key",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
    )
    tenants = plinth.tenants_list()
    ids = {t["id"] for t in tenants}
    assert {"default", "acme"} <= ids
    plinth.close()


def test_tenant_model_from_workspace_response():
    """SDK ``Tenant`` model accepts the workspace-style payload (id + count)."""

    from plinth.models import Tenant

    t = Tenant(id="default", workspace_count=3)
    assert t.id == "default"
    assert t.workspace_count == 3


def test_tenant_model_from_identity_response():
    """SDK ``Tenant`` model also accepts the identity-style payload."""

    from plinth.models import Tenant

    t = Tenant(
        id="acme",
        name="Acme",
        metadata={"plan": "free"},
        created_at=datetime.now(UTC),
    )
    assert t.name == "Acme"
    assert t.metadata == {"plan": "free"}


# ---------------------------------------------------------------------------
# v0.4 — Signing keys


def _signing_key_dict(*, kid: str = "abc1234567890def", active: bool = True) -> dict:
    return {
        "kid": kid,
        "alg": "RS256",
        "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n",
        "created_at": datetime.now(UTC).isoformat(),
        "rotated_in_at": datetime.now(UTC).isoformat() if active else None,
        "expires_at": datetime.now(UTC).isoformat(),
        "active": active,
    }


def test_list_keys_round_trips(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/keys").respond(
        200,
        json={"keys": [_signing_key_dict(kid="key-1"), _signing_key_dict(kid="key-2", active=False)]},
    )
    client = _identity_client(identity_mock)
    keys = client.list_keys()
    assert len(keys) == 2
    assert keys[0].kid == "key-1"
    assert keys[0].active is True
    assert keys[1].active is False


def test_list_keys_passes_include_expired(identity_mock: respx.MockRouter):
    route = identity_mock.get("/v1/keys").respond(200, json={"keys": []})
    client = _identity_client(identity_mock)
    client.list_keys(include_expired=True)
    request = route.calls.last.request
    assert "include_expired=true" in str(request.url)


def test_list_keys_empty_for_hs256(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/keys").respond(200, json={"keys": []})
    client = _identity_client(identity_mock)
    assert client.list_keys() == []


def test_rotate_key_returns_new_active(identity_mock: respx.MockRouter):
    identity_mock.post("/v1/keys/rotate").respond(
        201,
        json=_signing_key_dict(kid="new-active"),
    )
    client = _identity_client(identity_mock)
    new_key = client.rotate_key()
    assert new_key.kid == "new-active"
    assert new_key.active is True


def test_rotate_key_in_hs256_raises(identity_mock: respx.MockRouter):
    """The HS256 deployment surfaces a 400 when callers ask to rotate."""

    identity_mock.post("/v1/keys/rotate").respond(
        400,
        json={
            "error": {
                "code": "INVALID_ARGUMENTS",
                "message": "key rotation is only available when jwt_alg=RS256",
                "details": {"jwt_alg": "HS256"},
            }
        },
    )
    client = _identity_client(identity_mock)
    from plinth import InvalidArguments

    with pytest.raises(InvalidArguments):
        client.rotate_key()


def test_expire_key_returns_none(identity_mock: respx.MockRouter):
    identity_mock.delete("/v1/keys/abc123").respond(204)
    client = _identity_client(identity_mock)
    assert client.expire_key("abc123") is None


def test_expire_key_unknown_raises(identity_mock: respx.MockRouter):
    identity_mock.delete("/v1/keys/nope").respond(
        404,
        json={
            "error": {
                "code": "SIGNING_KEY_NOT_FOUND",
                "message": "Signing key 'nope' does not exist",
                "details": {"kid": "nope"},
            }
        },
    )
    client = _identity_client(identity_mock)
    from plinth import PlinthError

    with pytest.raises(PlinthError):
        client.expire_key("nope")


def test_get_key_returns_match(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/keys").respond(
        200,
        json={
            "keys": [
                _signing_key_dict(kid="wanted"),
                _signing_key_dict(kid="other", active=False),
            ]
        },
    )
    client = _identity_client(identity_mock)
    key = client.get_key("wanted")
    assert key.kid == "wanted"


def test_get_key_unknown_raises(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/keys").respond(200, json={"keys": []})
    client = _identity_client(identity_mock)
    from plinth import PlinthError

    with pytest.raises(PlinthError):
        client.get_key("does-not-exist")


def test_signing_key_model_round_trips():
    """The SDK ``SigningKey`` Pydantic model accepts identity payloads."""

    from plinth import SigningKey

    payload = _signing_key_dict()
    key = SigningKey.model_validate(payload)
    assert key.kid == payload["kid"]
    assert key.alg == "RS256"
    assert key.active is True
    # Public material round-trips. Private material is never present.
    assert "private" not in key.public_key_pem.lower()


# ---------------------------------------------------------------------------
# v0.6 — federated revocation


def _revocation_entry(
    *,
    jti: str,
    revoked_at: datetime | None = None,
    agent_id: str = "agt_1",
    tenant_id: str = "default",
) -> dict:
    return {
        "jti": jti,
        "revoked_at": (revoked_at or datetime.now(UTC)).isoformat(),
        "agent_id": agent_id,
        "tenant_id": tenant_id,
    }


def test_list_revocations_empty(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/revocations").respond(
        200,
        json={"revocations": [], "next_since": 0, "has_more": False},
    )
    client = _identity_client(identity_mock)
    page = client.list_revocations(since=0)
    assert page.revocations == []
    assert page.next_since == 0
    assert page.has_more is False


def test_list_revocations_passes_since_and_limit(identity_mock: respx.MockRouter):
    route = identity_mock.get("/v1/revocations").respond(
        200,
        json={"revocations": [], "next_since": 1700, "has_more": False},
    )
    client = _identity_client(identity_mock)
    client.list_revocations(since=1234, limit=42)
    request = route.calls.last.request
    qs = str(request.url)
    assert "since=1234" in qs
    assert "limit=42" in qs


def test_list_revocations_round_trips_entries(identity_mock: respx.MockRouter):
    rev_at = datetime.now(UTC)
    identity_mock.get("/v1/revocations").respond(
        200,
        json={
            "revocations": [
                _revocation_entry(jti="jti_a", revoked_at=rev_at, tenant_id="acme"),
                _revocation_entry(jti="jti_b", revoked_at=rev_at, tenant_id="acme"),
            ],
            "next_since": int(rev_at.timestamp()),
            "has_more": False,
        },
    )
    client = _identity_client(identity_mock)
    page = client.list_revocations(since=0)
    assert [e.jti for e in page.revocations] == ["jti_a", "jti_b"]
    assert all(e.tenant_id == "acme" for e in page.revocations)
    assert page.next_since == int(rev_at.timestamp())


def test_iter_revocations_follows_pagination(identity_mock: respx.MockRouter):
    """The iterator transparently follows ``has_more`` cursors."""

    page_one = {
        "revocations": [_revocation_entry(jti=f"jti_{n}") for n in range(3)],
        "next_since": 100,
        "has_more": True,
    }
    page_two = {
        "revocations": [_revocation_entry(jti=f"jti_{n}") for n in range(3, 5)],
        "next_since": 200,
        "has_more": False,
    }

    # Respx route ordering: first call → page one, second → page two.
    responses = iter([page_one, page_two])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    identity_mock.get("/v1/revocations").mock(side_effect=handler)
    client = _identity_client(identity_mock)
    seen = [e.jti for e in client.iter_revocations(since=0, page_size=3)]
    assert seen == [f"jti_{n}" for n in range(5)]


def test_iter_revocations_stops_on_non_advancing_cursor(
    identity_mock: respx.MockRouter,
):
    """Defensive: bail out if the server keeps returning the same cursor."""

    identity_mock.get("/v1/revocations").respond(
        200,
        json={
            "revocations": [],
            # Server returns the same cursor we passed in.
            "next_since": 0,
            "has_more": True,
        },
    )
    client = _identity_client(identity_mock)
    seen = list(client.iter_revocations(since=0))
    assert seen == []


def test_revocation_stats_returns_dict(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/revocations/stats").respond(
        200,
        json={"total": 7, "since_24h": 4, "since_1h": 2},
    )
    client = _identity_client(identity_mock)
    stats = client.revocation_stats()
    assert stats == {"total": 7, "since_24h": 4, "since_1h": 2}


def test_revocation_models_round_trip():
    """The ``RevocationEntry`` + ``RevocationList`` models accept identity payloads."""

    from plinth import RevocationEntry, RevocationList

    rev_at = datetime.now(UTC)
    entry = RevocationEntry.model_validate(
        _revocation_entry(jti="jti_x", revoked_at=rev_at, tenant_id="acme")
    )
    assert entry.jti == "jti_x"
    assert entry.tenant_id == "acme"

    page = RevocationList.model_validate(
        {
            "revocations": [
                _revocation_entry(jti="jti_x", revoked_at=rev_at),
                _revocation_entry(jti="jti_y", revoked_at=rev_at),
            ],
            "next_since": int(rev_at.timestamp()),
            "has_more": True,
        }
    )
    assert len(page.revocations) == 2
    assert page.has_more is True
    assert page.next_since == int(rev_at.timestamp())
