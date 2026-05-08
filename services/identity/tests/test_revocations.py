# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.6 federated-revocation endpoints + store helpers.

These cover ``GET /v1/revocations`` (page + cursor) and
``GET /v1/revocations/stats`` (counters), as well as the underlying
:meth:`TokenStore.list_revocations` / :meth:`TokenStore.revocation_stats`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from plinth_identity.store import TokenStore

UTC = timezone.utc


async def _seed(
    store: TokenStore,
    jti: str,
    *,
    agent_id: str = "agt",
    tenant_id: str = "default",
) -> None:
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    await store.insert(
        jti=jti,
        agent_id=agent_id,
        tenant_id=tenant_id,
        workspace_id=None,
        scopes=[],
        issued_at=issued_at,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Store-level


@pytest.mark.asyncio
async def test_list_revocations_empty_when_nothing_revoked(store: TokenStore):
    entries, has_more = await store.list_revocations(since_unix=0, limit=100)
    assert entries == []
    assert has_more is False


@pytest.mark.asyncio
async def test_list_revocations_returns_revoked_entries(store: TokenStore):
    for n in range(5):
        await _seed(store, f"jti_{n}", agent_id=f"agt_{n}", tenant_id="acme")
        await store.revoke(f"jti_{n}")

    entries, has_more = await store.list_revocations(since_unix=0, limit=100)
    assert len(entries) == 5
    assert has_more is False
    jtis = {e.jti for e in entries}
    assert jtis == {f"jti_{n}" for n in range(5)}
    for e in entries:
        assert e.tenant_id == "acme"
        assert e.revoked_at is not None


@pytest.mark.asyncio
async def test_list_revocations_filters_by_since(store: TokenStore):
    await _seed(store, "jti_old")
    await store.revoke("jti_old")

    # Sleep to ensure the second revocation has a strictly later timestamp.
    await asyncio.sleep(1.1)
    pivot = int(datetime.now(UTC).timestamp())
    await asyncio.sleep(1.1)

    await _seed(store, "jti_new")
    await store.revoke("jti_new")

    entries, has_more = await store.list_revocations(since_unix=pivot, limit=100)
    jtis = [e.jti for e in entries]
    assert jtis == ["jti_new"]
    assert has_more is False


@pytest.mark.asyncio
async def test_list_revocations_has_more_flag(store: TokenStore):
    for n in range(7):
        await _seed(store, f"jti_p{n}")
        await store.revoke(f"jti_p{n}")

    entries, has_more = await store.list_revocations(since_unix=0, limit=3)
    assert len(entries) == 3
    assert has_more is True

    # Total <= limit → has_more False.
    entries2, has_more2 = await store.list_revocations(since_unix=0, limit=20)
    assert len(entries2) == 7
    assert has_more2 is False


@pytest.mark.asyncio
async def test_list_revocations_orders_ascending_by_revoked_at(store: TokenStore):
    await _seed(store, "jti_a")
    await store.revoke("jti_a")
    await asyncio.sleep(1.1)
    await _seed(store, "jti_b")
    await store.revoke("jti_b")

    entries, _ = await store.list_revocations(since_unix=0, limit=10)
    # Oldest first (ascending) so cursor pagination is deterministic.
    assert [e.jti for e in entries] == ["jti_a", "jti_b"]
    assert entries[0].revoked_at <= entries[1].revoked_at


@pytest.mark.asyncio
async def test_revocation_stats_counts(store: TokenStore):
    # Two revocations, one current.
    for n in range(3):
        await _seed(store, f"jti_s{n}")
    await store.revoke("jti_s0")
    await store.revoke("jti_s1")
    total, since_24h, since_1h = await store.revocation_stats()
    assert total == 2
    assert since_24h == 2
    assert since_1h == 2


# ---------------------------------------------------------------------------
# HTTP-level


@pytest.mark.asyncio
async def test_get_revocations_endpoint_empty(client: httpx.AsyncClient):
    r = await client.get("/v1/revocations")
    assert r.status_code == 200
    body = r.json()
    assert body["revocations"] == []
    assert body["has_more"] is False
    assert body["next_since"] == 0


@pytest.mark.asyncio
async def test_get_revocations_after_revoke(client: httpx.AsyncClient):
    # Issue + revoke 5 tokens via the public API.
    jtis: list[str] = []
    for n in range(5):
        r = await client.post(
            "/v1/tokens",
            json={"agent_id": f"agt_{n}", "tenant_id": "acme", "scopes": []},
        )
        assert r.status_code == 201
        jti = r.json()["jti"]
        jtis.append(jti)
        rev = await client.post(f"/v1/tokens/{jti}/revoke")
        assert rev.status_code == 204

    r = await client.get("/v1/revocations", params={"since": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] is False
    returned = {e["jti"] for e in body["revocations"]}
    assert returned == set(jtis)
    # Each entry should carry the tenant + agent we minted it with.
    for entry in body["revocations"]:
        assert entry["tenant_id"] == "acme"
        assert entry["jti"] in jtis


@pytest.mark.asyncio
async def test_get_revocations_cursor_pagination(client: httpx.AsyncClient):
    # 4 revoked tokens, page size = 2.
    jtis: list[str] = []
    for n in range(4):
        r = await client.post(
            "/v1/tokens",
            json={"agent_id": f"agt_p_{n}", "scopes": []},
        )
        jti = r.json()["jti"]
        jtis.append(jti)
        await client.post(f"/v1/tokens/{jti}/revoke")
        # Slight gap so the cursor advances meaningfully.
        await asyncio.sleep(1.05)

    seen: list[str] = []
    cursor = 0
    pages = 0
    while True:
        r = await client.get(
            "/v1/revocations", params={"since": cursor, "limit": 2}
        )
        assert r.status_code == 200
        body = r.json()
        seen.extend(e["jti"] for e in body["revocations"])
        cursor = body["next_since"]
        pages += 1
        if not body["has_more"]:
            break
        # Don't loop forever if the cursor isn't advancing.
        assert pages < 10
    assert set(seen) == set(jtis)
    assert pages >= 2  # we saw multiple pages


@pytest.mark.asyncio
async def test_get_revocations_limit_clamped(client: httpx.AsyncClient):
    # limit > max should be rejected (FastAPI maps validation → 400 here).
    r = await client.get("/v1/revocations", params={"since": 0, "limit": 5000})
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_get_revocations_no_auth_required(client: httpx.AsyncClient):
    """The list endpoint is read-only metadata for legitimate replicas."""

    # No Authorization header on this client.
    r = await client.get("/v1/revocations")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_get_revocations_stats_endpoint(client: httpx.AsyncClient):
    # Issue + revoke a couple of tokens.
    for n in range(2):
        r = await client.post(
            "/v1/tokens",
            json={"agent_id": f"agt_st_{n}", "scopes": []},
        )
        jti = r.json()["jti"]
        await client.post(f"/v1/tokens/{jti}/revoke")

    r = await client.get("/v1/revocations/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["since_24h"] == 2
    assert body["since_1h"] == 2


@pytest.mark.asyncio
async def test_get_revocations_stats_zero(client: httpx.AsyncClient):
    r = await client.get("/v1/revocations/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["since_24h"] == 0
    assert body["since_1h"] == 0


@pytest.mark.asyncio
async def test_get_revocations_response_excludes_unrevoked(
    client: httpx.AsyncClient,
):
    # One revoked, one not.
    r = await client.post("/v1/tokens", json={"agent_id": "agt_a", "scopes": []})
    revoked_jti = r.json()["jti"]
    await client.post(f"/v1/tokens/{revoked_jti}/revoke")
    r = await client.post("/v1/tokens", json={"agent_id": "agt_b", "scopes": []})
    live_jti = r.json()["jti"]

    r = await client.get("/v1/revocations")
    body = r.json()
    seen = {e["jti"] for e in body["revocations"]}
    assert revoked_jti in seen
    assert live_jti not in seen


@pytest.mark.asyncio
async def test_get_revocations_negative_since_rejected(client: httpx.AsyncClient):
    r = await client.get("/v1/revocations", params={"since": -1})
    assert r.status_code in (400, 422)
