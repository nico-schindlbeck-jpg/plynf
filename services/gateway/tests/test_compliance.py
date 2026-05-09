# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.0 GDPR admin endpoints on the gateway.

Covers:
  * ``GET /v1/admin/tenant/{id}/export-data`` returns JSONL of all
    tenant-scoped rows.
  * Secrets in ``oauth_connections`` are redacted.
  * ``DELETE /v1/admin/tenant/{id}/data`` hard-deletes everything tenant-
    scoped and reports counts.
  * The admin scope check fires when auth is enforced.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from plinth_gateway.api import create_app
from plinth_gateway.audit import AuditLog, AuditRecord
from plinth_gateway.compliance import GatewayComplianceStore
from plinth_gateway.settings import Settings


@pytest_asyncio.fixture()
async def admin_client(tmp_path: Path) -> AsyncIterator[tuple]:
    """Permissive + auth-not-required gateway client for admin tests."""

    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=False,
        auth_mode="permissive",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as c, app.router.lifespan_context(app):
        yield app, c


def _audit_record(tenant_id: str = "default", **overrides) -> AuditRecord:
    base = {
        "tool_id": "web.fetch",
        "arguments": {"url": "u"},
        "workspace_id": "ws_a",
        "agent_id": "ag_a",
        "arguments_hash": "h" * 64,
        "arguments_preview": '{"url":"u"}',
        "cached": False,
        "duration_ms": 50,
        "cost_estimate_usd": 0.0005,
        "result_hash": "r" * 64,
        "error": None,
        "tenant_id": tenant_id,
    }
    base.update(overrides)
    return AuditRecord(**base)


@pytest.mark.asyncio
async def test_compliance_store_export_emits_audit_jsonl(db) -> None:
    audit = AuditLog(db)
    await audit.record(_audit_record(tenant_id="alpha"))
    await audit.record(_audit_record(tenant_id="beta"))

    store = GatewayComplianceStore(db)
    lines = []
    async for line in store.export_jsonl("alpha"):
        lines.append(line)
    types = [json.loads(line)["type"] for line in lines]
    assert "audit_event" in types
    # Beta rows must not appear in alpha's export.
    for line in lines:
        payload = json.loads(line)
        if payload["type"] == "audit_event":
            assert payload["tenant_id"] == "alpha"


@pytest.mark.asyncio
async def test_compliance_store_redacts_oauth_secrets(db) -> None:
    """OAuth tokens must always come back as ``"REDACTED"``."""

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO oauth_connections (
            id, tenant_id, provider, user_id, user_login, scopes,
            access_token_encrypted, refresh_token_encrypted,
            expires_at, created_at, last_refreshed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "oc_1",
            "alpha",
            "github",
            "12345",
            "octocat",
            "[]",
            "SECRET_AT",
            "SECRET_RT",
            None,
            now_iso,
            None,
        ),
    )

    store = GatewayComplianceStore(db)
    lines = []
    async for line in store.export_jsonl("alpha"):
        lines.append(line)
    oauth_lines = [
        json.loads(line)
        for line in lines
        if json.loads(line)["type"] == "oauth_connection"
    ]
    assert len(oauth_lines) == 1
    row = oauth_lines[0]
    assert row["access_token_encrypted"] == "REDACTED"
    assert row["refresh_token_encrypted"] == "REDACTED"
    # Non-secret fields are preserved.
    assert row["user_login"] == "octocat"


@pytest.mark.asyncio
async def test_compliance_store_delete_cascade_removes_rows(db) -> None:
    audit = AuditLog(db)
    await audit.record(_audit_record(tenant_id="alpha"))
    await audit.record(_audit_record(tenant_id="alpha"))
    await audit.record(_audit_record(tenant_id="beta"))

    store = GatewayComplianceStore(db)
    counts = await store.delete_tenant_data("alpha")
    assert counts["audit_events"] == 2

    # Beta rows still present.
    rows = await db.fetchall(
        "SELECT COUNT(*) AS c FROM audit_events WHERE tenant_id = ?",
        ("beta",),
    )
    assert int(rows[0]["c"]) == 1

    # Alpha rows fully gone.
    rows = await db.fetchall(
        "SELECT COUNT(*) AS c FROM audit_events WHERE tenant_id = ?",
        ("alpha",),
    )
    assert int(rows[0]["c"]) == 0


@pytest.mark.asyncio
async def test_admin_export_endpoint_returns_jsonl(admin_client) -> None:
    app, client = admin_client
    db = app.state.db
    audit = AuditLog(db)
    await audit.record(_audit_record(tenant_id="alpha"))

    resp = await client.get("/v1/admin/tenant/alpha/export-data")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/jsonl")
    lines = resp.text.strip().split("\n")
    assert any(
        '"type": "audit_event"' in line or '"type":"audit_event"' in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_admin_delete_endpoint_returns_counts(admin_client) -> None:
    app, client = admin_client
    db = app.state.db
    audit = AuditLog(db)
    await audit.record(_audit_record(tenant_id="alpha"))
    await audit.record(_audit_record(tenant_id="alpha"))

    resp = await client.delete("/v1/admin/tenant/alpha/data")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "deleted" in body
    assert body["deleted"]["audit_events"] == 2

    # Verify rows are gone.
    rows = await db.fetchall(
        "SELECT COUNT(*) AS c FROM audit_events WHERE tenant_id = ?",
        ("alpha",),
    )
    assert int(rows[0]["c"]) == 0


@pytest.mark.asyncio
async def test_admin_export_requires_admin_in_strict_mode(tmp_path) -> None:
    """Strict-auth deployments without admin scope must reject the call."""

    from httpx import ASGITransport, AsyncClient

    from plinth_gateway.api import create_app

    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        auth_mode="verify_local",
        identity_jwt_secret="abc" * 16,
        inbound_auth_required=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client, app.router.lifespan_context(app):
        # No Authorization header → no scopes → admin path rejected.
        resp = await client.get("/v1/admin/tenant/alpha/export-data")
        assert resp.status_code == 401, resp.text
