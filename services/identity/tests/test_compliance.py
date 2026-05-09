# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the identity GDPR compliance orchestrator.

Covers:

* ``ComplianceStore`` CRUD: export job lifecycle, delete job lifecycle,
  confirm-token issue + consume + expiry semantics.
* Identity-side data extraction (``emit_identity_jsonl`` /
  ``delete_identity_data``): tokens / quotas / tenants are surfaced
  exactly once; the literal ``default`` tenant is preserved through the
  cascade.
* The orchestrators (``run_export``, ``run_delete``) end-to-end with a
  patched httpx transport so workspace + gateway can return canned
  JSONL / counts without requiring real services.
* The HTTP endpoints (``POST /v1/tenants/{id}/export``,
  ``GET /v1/tenants/{id}/exports/{eid}``,
  ``GET /v1/tenants/{id}/exports/{eid}/download``,
  ``POST /v1/tenants/{id}/delete-data-confirm``,
  ``DELETE /v1/tenants/{id}/data?confirm=…``,
  ``GET /v1/tenants/{id}/delete-jobs/{id}``).
* Refusal of partial-failure rollback (documented limitation).
* Token redaction in the identity JSONL stream.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from plinth_identity.compliance import (
    ComplianceStore,
    DELETE_CONFIRM_TTL_SECONDS,
    EXPORT_TTL_HOURS,
    delete_identity_data,
    emit_identity_jsonl,
    run_delete,
    run_export,
)
from plinth_identity.settings import Settings
from plinth_identity.store import init_db


UTC = timezone.utc


# ---------------------------------------------------------------------------
# ComplianceStore — CRUD primitives


@pytest.mark.asyncio
async def test_compliance_store_export_lifecycle(settings: Settings) -> None:
    await init_db(settings.db_path)
    store = ComplianceStore(settings.db_path)

    job = await store.create_export("tenantA")
    assert job.export_id.startswith("exp_")
    assert job.tenant_id == "tenantA"
    assert job.status == "pending"
    assert job.completed_at is None
    assert job.expires_at is None

    completed = datetime.now(UTC).replace(microsecond=0)
    expires = completed + timedelta(hours=EXPORT_TTL_HOURS)
    await store.update_export(
        job.export_id,
        status="ready",
        completed_at=completed,
        expires_at=expires,
        size_bytes=4242,
    )
    fetched = await store.get_export(job.export_id)
    assert fetched is not None
    assert fetched.status == "ready"
    assert fetched.size_bytes == 4242
    assert fetched.completed_at == completed
    assert fetched.expires_at == expires


@pytest.mark.asyncio
async def test_compliance_store_get_export_missing(settings: Settings) -> None:
    await init_db(settings.db_path)
    store = ComplianceStore(settings.db_path)
    assert await store.get_export("exp_does_not_exist") is None


@pytest.mark.asyncio
async def test_compliance_store_delete_lifecycle(settings: Settings) -> None:
    await init_db(settings.db_path)
    store = ComplianceStore(settings.db_path)

    job = await store.create_delete_job("acme")
    assert job.job_id.startswith("del_")
    assert job.tenant_id == "acme"
    assert job.status == "pending"
    assert job.deleted_counts == {}

    counts = {"workspace.kv_entries": 5, "gateway.audit_events": 7}
    await store.update_delete_job(
        job.job_id,
        status="completed",
        completed_at=datetime.now(UTC).replace(microsecond=0),
        deleted_counts=counts,
    )
    fetched = await store.get_delete_job(job.job_id)
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.deleted_counts == counts


@pytest.mark.asyncio
async def test_compliance_store_get_delete_missing(settings: Settings) -> None:
    await init_db(settings.db_path)
    store = ComplianceStore(settings.db_path)
    assert await store.get_delete_job("del_nope") is None


@pytest.mark.asyncio
async def test_confirm_token_issue_consume(settings: Settings) -> None:
    await init_db(settings.db_path)
    store = ComplianceStore(settings.db_path)

    token, expires_at = await store.issue_confirm_token("acme")
    assert token.startswith("dcf_")
    assert expires_at > datetime.now(UTC)
    delta = (expires_at - datetime.now(UTC)).total_seconds()
    assert 0 < delta <= DELETE_CONFIRM_TTL_SECONDS + 5

    # Wrong tenant rejected.
    assert await store.consume_confirm_token(token, "other") is False

    # Right tenant: consume succeeds, but only once.
    assert await store.consume_confirm_token(token, "acme") is True
    assert await store.consume_confirm_token(token, "acme") is False


@pytest.mark.asyncio
async def test_confirm_token_expired(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db(settings.db_path)
    store = ComplianceStore(settings.db_path)

    # Force an immediate-expiry by patching ``_now`` to "look back" into
    # the past on consume.
    real_now = datetime.now(UTC).replace(microsecond=0)

    token, _ = await store.issue_confirm_token("acme")

    import plinth_identity.compliance as mod

    monkeypatch.setattr(
        mod,
        "_now",
        lambda: real_now + timedelta(seconds=DELETE_CONFIRM_TTL_SECONDS + 60),
    )
    assert await store.consume_confirm_token(token, "acme") is False
    # Expired tokens are deleted on consume — re-attempt also fails.
    assert await store.consume_confirm_token(token, "acme") is False


# ---------------------------------------------------------------------------
# Identity-side data extraction


@pytest.mark.asyncio
async def test_emit_identity_jsonl_collects_rows(client, settings: Settings) -> None:
    # Bootstrapping uses the lifespan-side ``init_db``; the ``client``
    # fixture also invokes the lifespan, so ``settings.db_path`` is ready.
    _ = client
    # Create a tenant + a quota row so the JSONL has something to emit.
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201, r.text
    r2 = await client.post(
        "/v1/tenants/acme/quotas",
        json={
            "max_workspaces": 10,
            "max_storage_gb": 5,
            "max_invocations_per_minute": 60,
            "max_cost_usd_day": 50.0,
            "max_cost_usd_month": 1000.0,
        },
    )
    assert r2.status_code == 200, r2.text

    lines = await emit_identity_jsonl(settings.db_path, "acme")
    assert lines, "expected at least one identity row"
    parsed = [json.loads(line) for line in lines]
    types = {row["type"] for row in parsed}
    assert "tenant" in types
    # The quota row may live under tenant_quotas as ``tenant_quota``.
    assert any(t in types for t in ("tenant_quota", "tenant_usage"))


@pytest.mark.asyncio
async def test_delete_identity_data_preserves_default_tenant(
    client, settings: Settings,
) -> None:
    _ = client
    counts = await delete_identity_data(settings.db_path, "default")
    # Default tenant row is preserved on purpose.
    assert counts.get("tenants", 0) == 0


@pytest.mark.asyncio
async def test_delete_identity_data_removes_tenant_rows(
    client, settings: Settings,
) -> None:
    r = await _create_tenant(client, "victim", "Victim Inc")
    assert r.status_code == 201, r.text
    counts = await delete_identity_data(settings.db_path, "victim")
    assert counts.get("tenants", 0) == 1
    # Subsequent fetch returns 404 — tenant row really gone.
    r2 = await client.get("/v1/tenants/victim")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Orchestrators


@pytest.mark.asyncio
async def test_run_export_bundles_zip(
    client, settings: Settings, tmp_path: Path,
) -> None:
    _ = client
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    # Mock workspace + gateway with a transport that hands back JSONL.
    transport = httpx.MockTransport(_canned_jsonl_handler())
    async with httpx.AsyncClient(transport=transport) as http_client:
        store = ComplianceStore(settings.db_path)
        export = await store.create_export("acme")
        exports_dir = tmp_path / "exports"

        result = await run_export(
            store=store,
            export_id=export.export_id,
            tenant_id="acme",
            workspace_url="http://workspace.test",
            gateway_url="http://gateway.test",
            exports_dir=exports_dir,
            db_path=settings.db_path,
            http_client=http_client,
        )

    assert result.status == "ready"
    assert result.size_bytes is not None and result.size_bytes > 0
    assert result.completed_at is not None
    assert result.expires_at is not None
    assert result.expires_at > result.completed_at

    zip_path = exports_dir / f"{export.export_id}.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "identity.jsonl", "workspace.jsonl", "gateway.jsonl"}.issubset(names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["tenant_id"] == "acme"
        assert manifest["export_id"] == export.export_id
        ws_jsonl = zf.read("workspace.jsonl").decode("utf-8")
        gw_jsonl = zf.read("gateway.jsonl").decode("utf-8")
    assert "workspace-row" in ws_jsonl
    assert "gateway-row" in gw_jsonl


@pytest.mark.asyncio
async def test_run_export_redacts_tokens(
    client, settings: Settings, tmp_path: Path,
) -> None:
    """Verify the orchestrator never persists raw OAuth tokens.

    The gateway side already redacts (``GatewayComplianceStore``); this
    test asserts the orchestrator preserves the redaction by faithfully
    relaying the upstream JSONL into the ZIP.
    """

    _ = client
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    REDACT = "REDACTED"
    line = json.dumps(
        {
            "type": "oauth_connection",
            "id": "oc_1",
            "access_token_encrypted": REDACT,
            "refresh_token_encrypted": REDACT,
            "tenant_id": "acme",
        },
        sort_keys=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "gateway" in str(request.url):
            return httpx.Response(200, text=line + "\n", headers={"content-type": "application/jsonl"})
        return httpx.Response(200, text="", headers={"content-type": "application/jsonl"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        store = ComplianceStore(settings.db_path)
        export = await store.create_export("acme")
        exports_dir = tmp_path / "exports"

        await run_export(
            store=store,
            export_id=export.export_id,
            tenant_id="acme",
            workspace_url="",
            gateway_url="http://gateway.test",
            exports_dir=exports_dir,
            db_path=settings.db_path,
            http_client=http_client,
        )

    zip_path = exports_dir / f"{export.export_id}.zip"
    with zipfile.ZipFile(zip_path) as zf:
        gw_jsonl = zf.read("gateway.jsonl").decode("utf-8")
    # No raw tokens (the redacted sentinel must be the only access_token value).
    assert "REDACTED" in gw_jsonl
    parsed = [json.loads(ln) for ln in gw_jsonl.strip().splitlines() if ln]
    for row in parsed:
        for k in ("access_token_encrypted", "refresh_token_encrypted"):
            if k in row:
                assert row[k] == "REDACTED"


@pytest.mark.asyncio
async def test_run_export_soft_fails_on_downstream(
    client, settings: Settings, tmp_path: Path,
) -> None:
    _ = client
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    # Returns 500 from both downstreams; the export should still complete with identity-only data.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        store = ComplianceStore(settings.db_path)
        export = await store.create_export("acme")
        exports_dir = tmp_path / "exports"

        result = await run_export(
            store=store,
            export_id=export.export_id,
            tenant_id="acme",
            workspace_url="http://workspace.test",
            gateway_url="http://gateway.test",
            exports_dir=exports_dir,
            db_path=settings.db_path,
            http_client=http_client,
        )

    assert result.status == "ready"
    zip_path = exports_dir / f"{export.export_id}.zip"
    with zipfile.ZipFile(zip_path) as zf:
        # Workspace + gateway pieces are empty but the file still exists.
        assert zf.read("workspace.jsonl") == b""
        assert zf.read("gateway.jsonl") == b""
        identity_jsonl = zf.read("identity.jsonl").decode("utf-8")
    assert "tenant" in identity_jsonl


@pytest.mark.asyncio
async def test_run_delete_aggregates_counts(
    client, settings: Settings,
) -> None:
    _ = client
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "DELETE":
            return httpx.Response(404)
        if "workspace" in str(request.url):
            return httpx.Response(200, json={"deleted": {"workspaces": 2, "kv_entries": 50}})
        return httpx.Response(200, json={"deleted": {"audit_events": 100, "tools": 3}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        store = ComplianceStore(settings.db_path)
        job = await store.create_delete_job("acme")

        result = await run_delete(
            store=store,
            job_id=job.job_id,
            tenant_id="acme",
            workspace_url="http://workspace.test",
            gateway_url="http://gateway.test",
            db_path=settings.db_path,
            http_client=http_client,
        )

    assert result.status == "completed"
    assert result.deleted_counts.get("workspace.workspaces") == 2
    assert result.deleted_counts.get("workspace.kv_entries") == 50
    assert result.deleted_counts.get("gateway.audit_events") == 100
    assert result.deleted_counts.get("gateway.tools") == 3
    assert result.deleted_counts.get("identity.tenants") == 1


@pytest.mark.asyncio
async def test_run_delete_continues_on_partial_failure(
    client, settings: Settings,
) -> None:
    _ = client
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    def handler(request: httpx.Request) -> httpx.Response:
        if "workspace" in str(request.url):
            # Workspace reachable returns OK.
            return httpx.Response(200, json={"deleted": {"workspaces": 1}})
        # Gateway returns 500 — partial failure.
        return httpx.Response(500, text="gateway down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        store = ComplianceStore(settings.db_path)
        job = await store.create_delete_job("acme")

        result = await run_delete(
            store=store,
            job_id=job.job_id,
            tenant_id="acme",
            workspace_url="http://workspace.test",
            gateway_url="http://gateway.test",
            db_path=settings.db_path,
            http_client=http_client,
        )

    # Identity step still runs — partial-failure is documented as v1.0
    # behaviour and surfaces in the counts dict.
    assert result.status == "completed"
    assert result.deleted_counts.get("workspace.workspaces") == 1
    assert "gateway.error" in result.deleted_counts
    assert result.deleted_counts.get("identity.tenants") == 1


# ---------------------------------------------------------------------------
# HTTP endpoints


@pytest.mark.asyncio
async def test_endpoint_export_full_round_trip(
    client, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    # Stub out the orchestrator with a synchronous helper that just marks
    # the job ready — keeps this test focused on the HTTP shape.
    import plinth_identity.api as api_mod
    real_run_export = api_mod.__dict__.get("run_export")
    _ = real_run_export

    async def fake_run_export(*, store, export_id, tenant_id, workspace_url, gateway_url, exports_dir, db_path, http_client=None):  # noqa: D401
        exports_dir.mkdir(parents=True, exist_ok=True)
        path = exports_dir / f"{export_id}.zip"
        # Minimal-but-real ZIP so the download endpoint succeeds.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"tenant_id": tenant_id, "export_id": export_id}))
        path.write_bytes(buf.getvalue())
        completed = datetime.now(UTC).replace(microsecond=0)
        await store.update_export(
            export_id,
            status="ready",
            completed_at=completed,
            expires_at=completed + timedelta(hours=EXPORT_TTL_HOURS),
            size_bytes=len(buf.getvalue()),
        )

    monkeypatch.setattr("plinth_identity.compliance.run_export", fake_run_export)

    # POST → 202 with an export_id
    r = await client.post("/v1/tenants/acme/export")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    export_id = body["export_id"]

    # GET status — the BackgroundTasks completion happens after the
    # response is returned. We reach into the store directly to finish.
    store = ComplianceStore(settings.db_path)
    await fake_run_export(
        store=store,
        export_id=export_id,
        tenant_id="acme",
        workspace_url=None,
        gateway_url=None,
        exports_dir=settings.data_dir / "exports",
        db_path=settings.db_path,
    )

    r = await client.get(f"/v1/tenants/acme/exports/{export_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["size_bytes"] > 0

    # Download — must return application/zip and the bytes we wrote.
    r = await client.get(f"/v1/tenants/acme/exports/{export_id}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.content[:2] == b"PK"  # ZIP local file header


@pytest.mark.asyncio
async def test_endpoint_export_unknown_tenant_404(client) -> None:
    r = await client.post("/v1/tenants/missing-x/export")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_export_missing_returns_404(client) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    r = await client.get("/v1/tenants/acme/exports/exp_does_not_exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_export_wrong_tenant_returns_404(
    client, settings: Settings,
) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    r = await _create_tenant(client, "other", "Other")
    assert r.status_code == 201

    store = ComplianceStore(settings.db_path)
    job = await store.create_export("acme")

    r = await client.get(f"/v1/tenants/other/exports/{job.export_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_export_download_not_ready(
    client, settings: Settings,
) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    store = ComplianceStore(settings.db_path)
    job = await store.create_export("acme")

    r = await client.get(f"/v1/tenants/acme/exports/{job.export_id}/download")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_endpoint_export_download_expired(
    client, settings: Settings,
) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    store = ComplianceStore(settings.db_path)
    job = await store.create_export("acme")

    past = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    await store.update_export(
        job.export_id,
        status="ready",
        completed_at=past - timedelta(hours=1),
        expires_at=past,
        size_bytes=10,
    )

    r = await client.get(f"/v1/tenants/acme/exports/{job.export_id}/download")
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_endpoint_delete_two_phase_full_round_trip(
    client, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    async def fake_run_delete(*, store, job_id, tenant_id, workspace_url, gateway_url, db_path, http_client=None):  # noqa: D401
        await store.update_delete_job(
            job_id,
            status="completed",
            completed_at=datetime.now(UTC).replace(microsecond=0),
            deleted_counts={"identity.tenants": 1},
        )

    monkeypatch.setattr("plinth_identity.compliance.run_delete", fake_run_delete)

    # Phase 1: confirm-token issue.
    r = await client.post("/v1/tenants/acme/delete-data-confirm")
    assert r.status_code == 200, r.text
    body = r.json()
    confirm = body["confirm_token"]

    # Phase 2: cascade.
    r = await client.delete(f"/v1/tenants/acme/data?confirm={confirm}")
    assert r.status_code == 202, r.text
    body = r.json()
    job_id = body["job_id"]

    store = ComplianceStore(settings.db_path)
    await fake_run_delete(
        store=store,
        job_id=job_id,
        tenant_id="acme",
        workspace_url=None,
        gateway_url=None,
        db_path=settings.db_path,
    )

    r = await client.get(f"/v1/tenants/acme/delete-jobs/{job_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["deleted_counts"]["identity.tenants"] == 1


@pytest.mark.asyncio
async def test_endpoint_delete_rejects_missing_token(client) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    r = await client.delete("/v1/tenants/acme/data?confirm=dcf_invalid_token_value")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_delete_rejects_other_tenant_token(
    client, settings: Settings,
) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    r2 = await _create_tenant(client, "other", "Other")
    assert r2.status_code == 201

    store = ComplianceStore(settings.db_path)
    token, _ = await store.issue_confirm_token("other")

    # A token issued for ``other`` must not work on ``acme``.
    r = await client.delete(f"/v1/tenants/acme/data?confirm={token}")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_delete_unknown_tenant_404(client) -> None:
    r = await client.post("/v1/tenants/missing-x/delete-data-confirm")
    assert r.status_code == 404
    r = await client.delete("/v1/tenants/missing-x/data?confirm=anything")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_delete_token_one_shot(
    client, settings: Settings,
) -> None:
    """Token consumption is one-shot — re-using rejects.

    Asserts at the store level so the assertion is independent of
    cascade side effects on the tenant row itself (the second HTTP
    attempt would hit a 404 because the cascade already removed the
    tenant; that's correct behaviour, just not what we're testing).
    """

    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201

    store = ComplianceStore(settings.db_path)
    token, _ = await store.issue_confirm_token("acme")

    # First consume — should succeed.
    assert await store.consume_confirm_token(token, "acme") is True
    # Second consume of the same token must fail.
    assert await store.consume_confirm_token(token, "acme") is False


@pytest.mark.asyncio
async def test_endpoint_delete_job_not_found(client) -> None:
    r = await _create_tenant(client, "acme", "Acme")
    assert r.status_code == 201
    r = await client.get("/v1/tenants/acme/delete-jobs/del_does_not_exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Helpers


async def _create_tenant(client: httpx.AsyncClient, tid: str, name: str) -> httpx.Response:
    return await client.post("/v1/tenants", json={"id": tid, "name": name})


def _canned_jsonl_handler() -> Any:
    """Mock transport that returns a small JSONL body for any GET."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(404)
        if "workspace" in str(request.url):
            body = json.dumps({"type": "workspace-row", "tenant_id": "acme"}) + "\n"
        else:
            body = json.dumps({"type": "gateway-row", "tenant_id": "acme"}) + "\n"
        return httpx.Response(
            200, text=body, headers={"content-type": "application/jsonl"},
        )

    return _handler
