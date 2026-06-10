# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Self-serve accounts: signup, login, key resolve, billing, webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from plinth_dashboard.accounts import AccountStore
from plinth_dashboard.billing_stripe import verify_webhook_signature
from plinth_dashboard.server import create_app
from plinth_dashboard.settings import get_settings


def make_client(**overrides) -> TestClient:
    settings = get_settings(**overrides)
    app = create_app(settings)
    return TestClient(app)


def _signup(client: TestClient, email="nico@example.com", password="hunter2secret"):
    return client.post(
        "/api/app/signup", json={"email": email, "password": password, "plan": "pro"}
    )


# -- signup / login ----------------------------------------------------------


def test_signup_returns_key_and_session():
    with make_client() as client:
        resp = _signup(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        acc = body["account"]
        assert acc["email"] == "nico@example.com"
        assert acc["api_key"].startswith("plynf_sk_live_")
        assert acc["tenant_id"].startswith("t_")
        # Paid plan is requested, but never granted before checkout.
        assert acc["plan"] == "free"
        assert acc["requested_plan"] == "pro"
        assert body["token"]  # session token (demo token offline)


def test_signup_duplicate_email_409():
    with make_client() as client:
        assert _signup(client).status_code == 200
        assert _signup(client).status_code == 409


def test_signup_validates_input():
    with make_client() as client:
        assert client.post(
            "/api/app/signup", json={"email": "not-an-email", "password": "longenough"}
        ).status_code == 400
        assert client.post(
            "/api/app/signup", json={"email": "a@b.co", "password": "short"}
        ).status_code == 400


def test_login_roundtrip_and_rejection():
    with make_client() as client:
        _signup(client)
        ok = client.post(
            "/api/app/login",
            json={"email": "nico@example.com", "password": "hunter2secret"},
        )
        assert ok.status_code == 200
        assert ok.json()["account"]["api_key"].startswith("plynf_sk_live_")
        bad = client.post(
            "/api/app/login",
            json={"email": "nico@example.com", "password": "wrong-password"},
        )
        assert bad.status_code == 401


def test_me_and_rotate():
    with make_client() as client:
        key1 = _signup(client).json()["account"]["api_key"]
        me = client.get("/api/app/me", params={"email": "nico@example.com"})
        assert me.status_code == 200
        assert me.json()["account"]["api_key"] == key1
        rotated = client.post("/api/app/keys/rotate", json={"email": "nico@example.com"})
        key2 = rotated.json()["account"]["api_key"]
        assert key2 != key1 and key2.startswith("plynf_sk_live_")


# -- internal key resolver (proxy auth source) -------------------------------


def test_internal_resolver_requires_secret():
    with make_client(internal_secret="s3cret") as client:
        key = _signup(client).json()["account"]["api_key"]
        assert client.get(f"/internal/keys/{key}").status_code == 403
        wrong = client.get(f"/internal/keys/{key}", headers={"X-Internal-Secret": "nope"})
        assert wrong.status_code == 403
        ok = client.get(f"/internal/keys/{key}", headers={"X-Internal-Secret": "s3cret"})
        assert ok.status_code == 200
        data = ok.json()
        assert data["tier"] == "free"
        assert data["tenant_id"].startswith("t_")


def test_internal_resolver_unknown_key_404():
    with make_client(internal_secret="s3cret") as client:
        resp = client.get(
            "/internal/keys/plynf_sk_live_doesnotexist",
            headers={"X-Internal-Secret": "s3cret"},
        )
        assert resp.status_code == 404


def test_internal_resolver_unconfigured_503():
    with make_client() as client:
        resp = client.get("/internal/keys/whatever", headers={"X-Internal-Secret": ""})
        assert resp.status_code == 503


# -- billing: simulated checkout (no Stripe configured) ----------------------


def test_simulated_checkout_upgrades_plan_and_resolver_sees_it():
    with make_client(internal_secret="s3cret") as client:
        key = _signup(client).json()["account"]["api_key"]
        resp = client.post(
            "/api/app/billing/checkout",
            json={"email": "nico@example.com", "plan": "pro"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["simulated"] is True
        assert "/app/welcome?checkout=success" in body["url"]
        # The proxy-facing resolver reflects the new tier immediately.
        resolved = client.get(
            f"/internal/keys/{key}", headers={"X-Internal-Secret": "s3cret"}
        ).json()
        assert resolved["tier"] == "pro"


def test_checkout_unknown_account_404():
    with make_client() as client:
        resp = client.post(
            "/api/app/billing/checkout", json={"email": "ghost@x.io", "plan": "pro"}
        )
        assert resp.status_code == 404


def test_portal_without_billing_profile_400():
    with make_client() as client:
        _signup(client)
        resp = client.post("/api/app/billing/portal", json={"email": "nico@example.com"})
        assert resp.status_code == 400


# -- Stripe webhook ----------------------------------------------------------


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def test_webhook_signature_verification():
    payload = b'{"type":"x"}'
    sig = _sign(payload, "whsec_test")
    assert verify_webhook_signature(payload, sig, "whsec_test")
    assert not verify_webhook_signature(payload, sig, "whsec_other")
    assert not verify_webhook_signature(b"tampered", sig, "whsec_test")
    old = _sign(payload, "whsec_test", ts=int(time.time()) - 4000)
    assert not verify_webhook_signature(payload, old, "whsec_test")


def test_webhook_checkout_completed_activates_plan():
    with make_client(stripe_webhook_secret="whsec_test", internal_secret="s3cret") as client:
        key = _signup(client).json()["account"]["api_key"]
        event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "nico@example.com",
                    "customer": "cus_123",
                    "subscription": "sub_456",
                    "metadata": {"plan": "pro"},
                }
            },
        }
        payload = json.dumps(event).encode()
        resp = client.post(
            "/api/stripe/webhook",
            content=payload,
            headers={"Stripe-Signature": _sign(payload, "whsec_test")},
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "activate"
        resolved = client.get(
            f"/internal/keys/{key}", headers={"X-Internal-Secret": "s3cret"}
        ).json()
        assert resolved["tier"] == "pro"


def test_webhook_bad_signature_rejected():
    with make_client(stripe_webhook_secret="whsec_test") as client:
        resp = client.post(
            "/api/stripe/webhook",
            content=b'{"type":"checkout.session.completed"}',
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert resp.status_code == 400


def test_webhook_subscription_deleted_downgrades():
    with make_client(stripe_webhook_secret="whsec_test", internal_secret="s3cret") as client:
        key = _signup(client).json()["account"]["api_key"]
        activate = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "nico@example.com",
                    "customer": "cus_123",
                    "subscription": "sub_456",
                    "metadata": {"plan": "pro"},
                }
            },
        }
        p1 = json.dumps(activate).encode()
        client.post(
            "/api/stripe/webhook",
            content=p1,
            headers={"Stripe-Signature": _sign(p1, "whsec_test")},
        )
        cancel = {
            "type": "customer.subscription.deleted",
            "data": {"object": {"customer": "cus_123"}},
        }
        p2 = json.dumps(cancel).encode()
        resp = client.post(
            "/api/stripe/webhook",
            content=p2,
            headers={"Stripe-Signature": _sign(p2, "whsec_test")},
        )
        assert resp.json()["action"] == "downgrade"
        resolved = client.get(
            f"/internal/keys/{key}", headers={"X-Internal-Secret": "s3cret"}
        ).json()
        assert resolved["tier"] == "free"


# -- persistence -------------------------------------------------------------


def test_account_store_persists_to_disk(tmp_path):
    path = tmp_path / "accounts.json"
    s1 = AccountStore(str(path))
    created = s1.create("p@q.de", "longpassword", "free")
    s2 = AccountStore(str(path))  # fresh load from disk
    assert s2.verify_login("p@q.de", "longpassword") is not None
    assert s2.resolve_key(created["api_key"])["tenant_id"] == created["tenant_id"]
