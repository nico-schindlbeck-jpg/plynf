# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Email verification + password reset: the account-store token methods, the
mailer backends, and the /api/app/* endpoints end-to-end.

Everything here runs with the in-memory AccountStore + ConsoleMailer, so it
needs no Postgres and no SMTP server. The Postgres backend is exercised by the
parallel tests in test_accounts_pg.py.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from plinth_dashboard.accounts import AccountError, AccountStore
from plinth_dashboard.mailer import (
    ConsoleMailer,
    SmtpMailer,
    send_password_reset_email,
    send_verification_email,
)
from plinth_dashboard.server import create_app
from plinth_dashboard.settings import get_settings

_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


def make_client(**overrides) -> TestClient:
    app = create_app(get_settings(**overrides))
    return TestClient(app)


def _signup(client: TestClient, email="n@example.com", password="longpassword"):
    return client.post("/api/app/signup", json={"email": email, "password": password})


def _token_from_outbox(client: TestClient, idx: int = -1) -> str:
    outbox = client.app.state.mailer.outbox
    return _TOKEN_RE.search(outbox[idx]["text"]).group(1)


# -- AccountStore token methods (in-memory) ----------------------------------


def test_new_account_starts_unverified():
    store = AccountStore()
    acc = store.create("a@b.co", "longpassword")
    assert acc["verified"] is False
    assert store.public_view(acc)["verified"] is False


def test_verification_roundtrip():
    store = AccountStore()
    store.create("a@b.co", "longpassword")
    token = store.issue_verification("a@b.co")
    assert token and isinstance(token, str)
    assert store.verify_email(token) == "a@b.co"
    assert store.get("a@b.co")["verified"] is True
    # Single-use: the token is cleared once consumed.
    assert store.verify_email(token) is None


def test_verification_unknown_email_and_bad_token():
    store = AccountStore()
    assert store.issue_verification("ghost@x.io") is None
    assert store.verify_email("not-a-real-token") is None


def test_verification_expired_token_rejected():
    store = AccountStore()
    store.create("a@b.co", "longpassword")
    token = store.issue_verification("a@b.co")
    # Force the stored expiry into the past.
    store._accounts["a@b.co"]["verify_token_expires"] = time.time() - 1
    assert store.verify_email(token) is None
    assert store.get("a@b.co")["verified"] is False


def test_password_reset_roundtrip_changes_login():
    store = AccountStore()
    store.create("a@b.co", "oldpassword")
    token = store.issue_password_reset("a@b.co")
    assert token
    assert store.reset_password(token, "brandnewpassword") == "a@b.co"
    assert store.verify_login("a@b.co", "brandnewpassword") is not None
    assert store.verify_login("a@b.co", "oldpassword") is None
    # Single-use.
    assert store.reset_password(token, "anotherpassword123") is None


def test_password_reset_unknown_email_and_weak_password():
    store = AccountStore()
    store.create("a@b.co", "oldpassword")
    assert store.issue_password_reset("ghost@x.io") is None
    token = store.issue_password_reset("a@b.co")
    with pytest.raises(AccountError):
        store.reset_password(token, "short")
    # A rejected weak password must not consume the token.
    assert store.reset_password(token, "longenoughpassword") == "a@b.co"


def test_password_reset_expired_token_rejected():
    store = AccountStore()
    store.create("a@b.co", "oldpassword")
    token = store.issue_password_reset("a@b.co")
    store._accounts["a@b.co"]["reset_token_expires"] = time.time() - 1
    assert store.reset_password(token, "brandnewpassword") is None
    assert store.verify_login("a@b.co", "oldpassword") is not None  # unchanged


def test_tokens_stored_hashed_never_plaintext(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(str(path))
    store.create("a@b.co", "longpassword")
    token = store.issue_password_reset("a@b.co")
    on_disk = path.read_text(encoding="utf-8")
    assert token not in on_disk  # only the hash is persisted
    assert store._accounts["a@b.co"]["reset_token_hash"] != token


# -- Mailer backends ---------------------------------------------------------


def test_console_mailer_records_and_links_are_correct():
    mailer = ConsoleMailer()
    assert send_verification_email(mailer, "https://plynf.com/", "a@b.co", "TOK1") is True
    assert send_password_reset_email(mailer, "https://plynf.com", "a@b.co", "TOK2") is True
    assert mailer.outbox[0]["to"] == "a@b.co"
    assert "https://plynf.com/app/verify?token=TOK1" in mailer.outbox[0]["text"]
    assert "https://plynf.com/app/reset?token=TOK2" in mailer.outbox[1]["text"]


def test_send_is_best_effort_when_backend_raises():
    class _Boom:
        def send(self, *a, **k):
            raise RuntimeError("smtp down")

    # Must swallow the error and report failure, never propagate.
    assert send_verification_email(_Boom(), "https://plynf.com", "a@b.co", "T") is False


def test_smtp_mailer_uses_starttls_and_login(monkeypatch):
    sent: dict = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["addr"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr("plinth_dashboard.mailer.smtplib.SMTP", _FakeSMTP)
    SmtpMailer("smtp.test", 587, "user", "pw", True, "Plynf <no@plynf.com>").send(
        "a@b.co", "Subject", "Body"
    )
    assert sent["addr"] == ("smtp.test", 587)
    assert sent["tls"] is True
    assert sent["login"] == ("user", "pw")
    assert sent["msg"]["To"] == "a@b.co"
    assert sent["msg"]["From"] == "Plynf <no@plynf.com>"


# -- API endpoints (end-to-end through the ConsoleMailer) --------------------


def test_signup_sends_verification_then_confirm_marks_verified():
    with make_client() as client:
        r = _signup(client)
        assert r.status_code == 200
        assert r.json()["account"]["verified"] is False
        token = _token_from_outbox(client)
        confirm = client.post("/api/app/verify/confirm", json={"token": token})
        assert confirm.status_code == 200 and confirm.json()["verified"] is True
        me = client.get("/api/app/me", params={"email": "n@example.com"})
        assert me.json()["account"]["verified"] is True


def test_verify_confirm_rejects_bad_token():
    with make_client() as client:
        r = client.post("/api/app/verify/confirm", json={"token": "garbage"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_TOKEN"


def test_verify_request_resends_for_known_account():
    with make_client() as client:
        _signup(client)
        before = len(client.app.state.mailer.outbox)
        r = client.post("/api/app/verify/request", json={"email": "n@example.com"})
        assert r.status_code == 200
        assert len(client.app.state.mailer.outbox) == before + 1


def test_password_reset_flow_changes_login():
    with make_client() as client:
        client.post("/api/app/signup", json={"email": "n@example.com", "password": "oldpassword"})
        req = client.post("/api/app/password/reset/request", json={"email": "n@example.com"})
        assert req.status_code == 200
        token = _token_from_outbox(client)
        confirm = client.post(
            "/api/app/password/reset/confirm",
            json={"token": token, "password": "brandnewpassword"},
        )
        assert confirm.status_code == 200
        assert client.post(
            "/api/app/login", json={"email": "n@example.com", "password": "brandnewpassword"}
        ).status_code == 200
        assert client.post(
            "/api/app/login", json={"email": "n@example.com", "password": "oldpassword"}
        ).status_code == 401


def test_password_reset_request_unknown_email_is_opaque_200_with_no_mail():
    with make_client() as client:
        before = len(client.app.state.mailer.outbox)
        r = client.post("/api/app/password/reset/request", json={"email": "ghost@x.io"})
        assert r.status_code == 200  # never discloses non-existence
        assert len(client.app.state.mailer.outbox) == before  # nothing sent


def test_password_reset_confirm_rejects_bad_token_and_weak_password():
    with make_client() as client:
        _signup(client, password="oldpassword")
        client.post("/api/app/password/reset/request", json={"email": "n@example.com"})
        tok = _token_from_outbox(client)
        bad = client.post(
            "/api/app/password/reset/confirm", json={"token": "nope", "password": "longenough1"}
        )
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_TOKEN"
        weak = client.post(
            "/api/app/password/reset/confirm", json={"token": tok, "password": "short"}
        )
        assert weak.status_code == 400
