# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""PostgresAccountStore — exercised against a real Postgres (CI provides one
at PLINTH_TEST_POSTGRES_URL). Skips cleanly where psycopg or a DB is absent."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")
PG_URL = os.environ.get("PLINTH_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="no PLINTH_TEST_POSTGRES_URL")

from plinth_dashboard.accounts import AccountError  # noqa: E402
from plinth_dashboard.accounts_pg import PostgresAccountStore  # noqa: E402


@pytest.fixture
def store():
    s = PostgresAccountStore(PG_URL)  # bootstraps schema
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE accounts")  # clean slate per test
    return s


def test_create_and_get(store):
    a = store.create("Foo@Example.com ", "supersecret")
    assert a["email"] == "foo@example.com"  # normalised
    assert a["plan"] == "free" and a["api_key"].startswith("plynf_sk_live_")
    got = store.get("foo@example.com")
    assert got["tenant_id"] == a["tenant_id"]
    pv = store.public_view(got)
    assert "password_hash" not in pv and pv["api_key"] == a["api_key"]


def test_duplicate_email_raises(store):
    store.create("dup@example.com", "supersecret")
    with pytest.raises(AccountError):
        store.create("dup@example.com", "anotherpassword")


def test_verify_login(store):
    store.create("u@example.com", "rightpassword")
    assert store.verify_login("u@example.com", "rightpassword") is not None
    assert store.verify_login("u@example.com", "wrongpassword") is None
    assert store.verify_login("nobody@example.com", "whatever") is None  # equal-time path


def test_resolve_and_rotate_key(store):
    a = store.create("k@example.com", "supersecret")
    assert store.resolve_key(a["api_key"]) == {
        "tenant_id": a["tenant_id"],
        "tier": "free",
        "email": "k@example.com",
    }
    rotated = store.rotate_key("k@example.com")
    assert rotated["api_key"] != a["api_key"]
    assert store.resolve_key(a["api_key"]) is None  # old key is dead
    assert store.resolve_key(rotated["api_key"]) is not None


def test_set_plan_changes_resolved_tier(store):
    a = store.create("p@example.com", "supersecret")
    store.set_plan("p@example.com", "pro")
    assert store.resolve_key(a["api_key"])["tier"] == "pro"
    with pytest.raises(AccountError):
        store.set_plan("p@example.com", "platinum")


def test_stripe_attach_partial_update_and_lookup(store):
    store.create("s@example.com", "supersecret")
    store.attach_stripe("s@example.com", customer_id="cus_123", subscription_id="sub_456")
    assert store.email_for_customer("cus_123") == "s@example.com"
    assert store.public_view(store.get("s@example.com"))["has_billing"] is True
    # a None arg must leave the existing value intact (COALESCE semantics)
    store.attach_stripe("s@example.com", subscription_id="sub_789")
    got = store.get("s@example.com")
    assert got["stripe_customer_id"] == "cus_123"
    assert got["stripe_subscription_id"] == "sub_789"


def test_persists_across_instances(store):
    a = store.create("persist@example.com", "supersecret")
    # a fresh store (new connections) sees the same row — the whole point of PG.
    other = PostgresAccountStore(PG_URL)
    got = other.get("persist@example.com")
    assert got is not None and got["api_key"] == a["api_key"]
