# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Authorization regression tests for the self-serve account routes.

Guards the fix for the cross-account API-key disclosure: with auth enforced,
sensitive routes (/me, /keys/rotate, /billing/*) must operate ONLY on the
account behind the verified session token — never on a client-supplied email.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from plinth_dashboard.server import create_app
from plinth_dashboard.settings import get_settings


class _FakeResp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeIdentityClient:
    """Stateless stand-in for the identity service.

    Mints tokens shaped ``tok-<email>`` and verifies them back to that email,
    so it works for any number of distinct users in one test.
    """

    async def post(self, url: str, json: dict | None = None):
        body = json or {}
        if url.endswith("/v1/tokens"):
            sub = str(body.get("agent_id") or "")
            return _FakeResp(200, {"token": f"tok-{sub}", "claims": {"agent_id": sub}})
        if url.endswith("/v1/tokens/verify"):
            tok = str(body.get("token") or "")
            email = tok[4:] if tok.startswith("tok-") else ""
            return _FakeResp(200, {"agent_id": email}) if email else _FakeResp(401, {})
        return _FakeResp(404, {})


class _FakeOverview:
    def __init__(self) -> None:
        self.client = _FakeIdentityClient()

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _enforced_client() -> TestClient:
    settings = get_settings(app_auth_required=True)
    app = create_app(settings, overview=_FakeOverview())
    return TestClient(app)


def _signup(client, email):
    return client.post(
        "/api/app/signup", json={"email": email, "password": "longpassword1"}
    ).json()


def test_cross_account_me_is_bound_to_session_not_query_email():
    with _enforced_client() as client:
        alice = _signup(client, "alice@example.com")
        bob = _signup(client, "bob@example.com")
        alice_token = alice["token"]
        bob_key = bob["account"]["api_key"]

        # Alice, authenticated, asks for BOB's account by email.
        r = client.get(
            "/api/app/me?email=bob@example.com",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert r.status_code == 200
        # She gets her OWN account — never bob's key.
        assert r.json()["account"]["email"] == "alice@example.com"
        assert r.json()["account"]["api_key"] != bob_key


def test_me_without_token_is_rejected_when_enforced():
    with _enforced_client() as client:
        _signup(client, "alice@example.com")
        r = client.get("/api/app/me?email=alice@example.com")
        assert r.status_code in (401, 404)


def test_key_rotation_cannot_target_another_account():
    with _enforced_client() as client:
        _signup(client, "alice@example.com")
        bob = _signup(client, "bob@example.com")
        alice_token = client.post(
            "/api/app/login",
            json={"email": "alice@example.com", "password": "longpassword1"},
        ).json()["token"]
        bob_key_before = bob["account"]["api_key"]

        # Alice tries to rotate bob's key.
        r = client.post(
            "/api/app/keys/rotate",
            json={"email": "bob@example.com"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        # The rotation targets ALICE (session identity), not bob.
        assert r.json()["account"]["email"] == "alice@example.com"

        # Bob's key is unchanged — verify via his own session.
        bob_token = client.post(
            "/api/app/login",
            json={"email": "bob@example.com", "password": "longpassword1"},
        ).json()["token"]
        bob_now = client.get(
            "/api/app/me?email=bob@example.com",
            headers={"Authorization": f"Bearer {bob_token}"},
        ).json()
        assert bob_now["account"]["api_key"] == bob_key_before


def test_billing_checkout_cannot_be_triggered_for_another_account():
    with _enforced_client() as client:
        _signup(client, "alice@example.com")
        _signup(client, "victim@example.com")
        alice_token = client.post(
            "/api/app/login",
            json={"email": "alice@example.com", "password": "longpassword1"},
        ).json()["token"]

        # Simulated billing (no Stripe): a checkout flips the plan. Alice must
        # not be able to start one for the victim.
        client.post(
            "/api/app/billing/checkout",
            json={"email": "victim@example.com", "plan": "pro"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        # Victim still on free (own session check).
        victim_token = client.post(
            "/api/app/login",
            json={"email": "victim@example.com", "password": "longpassword1"},
        ).json()["token"]
        victim = client.get(
            "/api/app/me?email=victim@example.com",
            headers={"Authorization": f"Bearer {victim_token}"},
        ).json()
        assert victim["account"]["plan"] == "free"
