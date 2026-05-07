# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Unit tests for jwt issue + verify primitives."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from plinth_identity.exceptions import InvalidArguments, InvalidToken, TokenExpired
from plinth_identity.jwt_io import JWT_ALG, TokenManager, new_jti

UTC = timezone.utc


def test_new_jti_format():
    jti = new_jti()
    assert jti.startswith("jti_")
    assert len(jti) > 4


def test_token_manager_requires_secret():
    with pytest.raises(InvalidArguments):
        TokenManager(secret="", issuer="http://x", audience="plinth")


def test_issue_then_decode_roundtrips_claims(token_manager: TokenManager):
    issued = token_manager.issue(
        agent_id="agt_1",
        tenant_id="acme",
        scopes=["tool:web.fetch:read"],
        workspace_id="ws_x",
        ttl_seconds=300,
    )
    claims = token_manager.decode(issued.token)
    assert claims.sub == "agt_1"
    assert claims.agent_id == "agt_1"
    assert claims.tenant_id == "acme"
    assert claims.workspace_id == "ws_x"
    assert claims.scopes == ["tool:web.fetch:read"]
    assert claims.iss == token_manager.issuer
    assert claims.aud == token_manager.audience
    assert claims.exp > claims.iat


def test_issue_assigns_jti_when_not_provided(token_manager: TokenManager):
    issued = token_manager.issue(
        agent_id="agt_2",
        tenant_id="default",
        scopes=[],
    )
    assert issued.claims.jti.startswith("jti_")


def test_issue_uses_provided_jti(token_manager: TokenManager):
    issued = token_manager.issue(
        agent_id="agt_3",
        tenant_id="default",
        scopes=[],
        jti="jti_explicit",
    )
    assert issued.claims.jti == "jti_explicit"


def test_issue_rejects_zero_or_negative_ttl(token_manager: TokenManager):
    with pytest.raises(InvalidArguments):
        token_manager.issue(agent_id="x", tenant_id="t", scopes=[], ttl_seconds=0)
    with pytest.raises(InvalidArguments):
        token_manager.issue(agent_id="x", tenant_id="t", scopes=[], ttl_seconds=-1)


def test_decode_expired_token_raises(token_manager: TokenManager):
    issued = token_manager.issue(
        agent_id="agt_x",
        tenant_id="t",
        scopes=[],
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(TokenExpired):
        token_manager.decode(issued.token)


def test_decode_tampered_signature_raises(token_manager: TokenManager):
    issued = token_manager.issue(
        agent_id="agt_x",
        tenant_id="t",
        scopes=[],
    )
    # Mutate the last char of the signature.
    parts = issued.token.split(".")
    tampered = ".".join([*parts[:2], parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")])
    with pytest.raises(InvalidToken):
        token_manager.decode(tampered)


def test_decode_wrong_audience_raises(token_manager: TokenManager):
    bad = pyjwt.encode(
        {
            "sub": "x",
            "iss": token_manager.issuer,
            "aud": "not-plinth",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "jti_x",
        },
        "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA=",
        algorithm=JWT_ALG,
    )
    with pytest.raises(InvalidToken):
        token_manager.decode(bad)


def test_decode_wrong_issuer_raises(token_manager: TokenManager):
    bad = pyjwt.encode(
        {
            "sub": "x",
            "iss": "http://other-issuer",
            "aud": token_manager.audience,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "jti_x",
        },
        "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA=",
        algorithm=JWT_ALG,
    )
    with pytest.raises(InvalidToken):
        token_manager.decode(bad)


def test_decode_garbage_raises(token_manager: TokenManager):
    with pytest.raises(InvalidToken):
        token_manager.decode("not-a-jwt-at-all")


def test_decode_unverified_returns_payload(token_manager: TokenManager):
    issued = token_manager.issue(agent_id="x", tenant_id="t", scopes=[])
    payload = token_manager.decode_unverified(issued.token)
    assert payload["agent_id"] == "x"


def test_decode_unverified_garbage_raises(token_manager: TokenManager):
    with pytest.raises(InvalidToken):
        token_manager.decode_unverified("xyz")


def test_decode_with_missing_custom_claims_defaults(token_manager: TokenManager):
    """Tokens minted by an older issuer (no custom claims) still decode.

    We default ``agent_id``, ``tenant_id``, ``scopes`` so verifiers don't
    explode.
    """

    raw = pyjwt.encode(
        {
            "sub": "agt_old",
            "iss": token_manager.issuer,
            "aud": token_manager.audience,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "jti_old",
        },
        "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA=",
        algorithm=JWT_ALG,
    )
    claims = token_manager.decode(raw)
    assert claims.agent_id == "agt_old"
    assert claims.tenant_id == "default"
    assert claims.scopes == []
