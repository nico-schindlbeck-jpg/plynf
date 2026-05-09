# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for v1.0 SDK additions: quotas, usage, schema-wizard helpers."""

from __future__ import annotations

import httpx
import pytest
import respx

from plinth import IdentityClient
from plinth.identity import (
    TenantQuotas,
    TenantQuotasUpdate,
    TenantUsage,
)
from plinth.models import SchemaCheckResult


IDENTITY_URL = "http://identity.test"
WORKSPACE_URL = "http://workspace.test"


@pytest.fixture
def identity_mock() -> respx.MockRouter:
    with respx.mock(base_url=IDENTITY_URL, assert_all_called=False) as router:
        yield router


def _identity_client(router: respx.MockRouter) -> IdentityClient:
    return IdentityClient(
        IDENTITY_URL,
        api_key="test-key",
        transport=httpx.MockTransport(router.handler),
    )


def _full_quotas_payload(tenant_id: str = "acme", **overrides) -> dict:
    body = {
        "tenant_id": tenant_id,
        "max_workspaces": 100,
        "max_storage_gb": 10.0,
        "max_channels_per_workspace": 50,
        "max_workflows_per_workspace": 100,
        "max_active_tokens": 1000,
        "max_oauth_connections": 50,
        "max_cost_usd_day": 100.0,
        "max_cost_usd_month": 2000.0,
        "max_invocations_per_minute": 600,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# get_quotas / set_quotas / reset_quotas


def test_get_quotas_returns_full_envelope(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tenants/acme/quotas").mock(
        return_value=httpx.Response(200, json=_full_quotas_payload())
    )
    client = _identity_client(identity_mock)
    q = client.get_quotas("acme")
    assert isinstance(q, TenantQuotas)
    assert q.tenant_id == "acme"
    assert q.max_workspaces == 100
    assert q.max_cost_usd_day == 100.0


def test_set_quotas_with_partial_update(identity_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(200, json=_full_quotas_payload(max_workspaces=42))

    identity_mock.post("/v1/tenants/acme/quotas").mock(side_effect=handler)
    client = _identity_client(identity_mock)
    q = client.set_quotas(
        "acme",
        TenantQuotasUpdate(max_workspaces=42),
    )
    assert q.max_workspaces == 42
    assert captured == {"max_workspaces": 42}


def test_set_quotas_with_full_envelope(identity_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(200, json=_full_quotas_payload())

    identity_mock.post("/v1/tenants/acme/quotas").mock(side_effect=handler)
    client = _identity_client(identity_mock)
    full = TenantQuotas(tenant_id="acme", max_workspaces=7)
    client.set_quotas("acme", full)
    # `tenant_id` and `updated_at` are excluded from the body — the URL
    # is the source of truth for tenant_id.
    assert "tenant_id" not in captured
    assert "updated_at" not in captured
    assert captured["max_workspaces"] == 7


def test_set_quotas_with_dict(identity_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(200, json=_full_quotas_payload())

    identity_mock.post("/v1/tenants/acme/quotas").mock(side_effect=handler)
    client = _identity_client(identity_mock)
    client.set_quotas("acme", {"max_storage_gb": 25.0})
    assert captured == {"max_storage_gb": 25.0}


def test_reset_quotas_calls_delete(identity_mock: respx.MockRouter):
    route = identity_mock.delete("/v1/tenants/acme/quotas").mock(
        return_value=httpx.Response(204)
    )
    client = _identity_client(identity_mock)
    client.reset_quotas("acme")
    assert route.called


# ---------------------------------------------------------------------------
# get_usage


def test_get_usage_returns_envelope(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tenants/acme/usage").mock(
        return_value=httpx.Response(
            200,
            json={
                "tenant_id": "acme",
                "workspaces": 0,
                "storage_gb": 0.0,
                "active_tokens": 4,
                "oauth_connections": 0,
                "cost_usd_day": 0.0,
                "cost_usd_month": 0.0,
                "last_invocation_at": None,
                "notes": {"workspaces": "owned by workspace service"},
            },
        )
    )
    client = _identity_client(identity_mock)
    usage = client.get_usage("acme")
    assert isinstance(usage, TenantUsage)
    assert usage.active_tokens == 4
    assert "workspaces" in usage.notes


def test_get_usage_with_real_payload(identity_mock: respx.MockRouter):
    identity_mock.get("/v1/tenants/foo/usage").mock(
        return_value=httpx.Response(
            200,
            json={
                "tenant_id": "foo",
                "workspaces": 3,
                "storage_gb": 1.5,
                "active_tokens": 10,
                "oauth_connections": 2,
                "cost_usd_day": 0.25,
                "cost_usd_month": 7.5,
                "last_invocation_at": "2026-05-01T00:00:00Z",
                "notes": {},
            },
        )
    )
    client = _identity_client(identity_mock)
    usage = client.get_usage("foo")
    assert usage.cost_usd_day == 0.25
    assert usage.last_invocation_at is not None


# ---------------------------------------------------------------------------
# Schema wizard helper (channels.preview_schema_change)


def test_preview_schema_change_compatible(client, workspace_mock: respx.MockRouter):
    from tests.conftest import make_workspace

    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(
            200, json={"workspaces": [make_workspace(name="ws")]}
        )
    )

    workspace_mock.post(
        "/v1/workspaces/ws_01TESTWORKSPACE/channels/research-out/schema/check"
    ).mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "channel": "research-out",
                "scope": "main",
                "checked": 5,
                "valid": 5,
                "invalid": 0,
                "sample_failures": [],
            },
        )
    )

    ws = client.workspace("ws")
    report = ws.channels.preview_schema_change(
        "research-out", {"type": "object"}
    )
    assert report["compatible"] is True
    assert isinstance(report["main_check"], SchemaCheckResult)
    assert isinstance(report["deadletter_check"], SchemaCheckResult)
    assert "Safe" in report["recommendation"]


def test_preview_schema_change_incompatible(client, workspace_mock: respx.MockRouter):
    from tests.conftest import make_workspace

    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(
            200, json={"workspaces": [make_workspace(name="ws")]}
        )
    )

    # The check API is hit twice — once per scope.
    def handler(req):
        # Detect which scope from the body.
        import json as _json

        body = _json.loads(req.content)
        scope = body.get("scope", "both")
        if scope == "main":
            return httpx.Response(
                200,
                json={
                    "channel": "research-out",
                    "scope": "main",
                    "checked": 10,
                    "valid": 7,
                    "invalid": 3,
                    "sample_failures": [
                        {"msg_id": f"m{i}", "errors": ["bad"]} for i in range(3)
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "channel": "research-out",
                "scope": scope,
                "checked": 0,
                "valid": 0,
                "invalid": 0,
                "sample_failures": [],
            },
        )

    workspace_mock.post(
        "/v1/workspaces/ws_01TESTWORKSPACE/channels/research-out/schema/check"
    ).mock(side_effect=handler)

    ws = client.workspace("ws")
    report = ws.channels.preview_schema_change(
        "research-out", {"type": "object"}
    )
    assert report["compatible"] is False
    assert report["main_check"].invalid == 3
    assert "rewrite" in report["recommendation"].lower() or "migrate" in report["recommendation"].lower()


def test_preview_schema_change_dlq_has_pending_failures(
    client, workspace_mock: respx.MockRouter
):
    from tests.conftest import make_workspace

    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(
            200, json={"workspaces": [make_workspace(name="ws")]}
        )
    )

    def handler(req):
        import json as _json

        body = _json.loads(req.content)
        scope = body.get("scope", "both")
        if scope == "main":
            return httpx.Response(
                200,
                json={
                    "channel": "research-out",
                    "scope": "main",
                    "checked": 5,
                    "valid": 5,
                    "invalid": 0,
                    "sample_failures": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "channel": "research-out",
                "scope": scope,
                "checked": 8,
                "valid": 6,
                "invalid": 2,
                "sample_failures": [
                    {"msg_id": "dlq_a", "errors": ["bad"]},
                    {"msg_id": "dlq_b", "errors": ["bad"]},
                ],
            },
        )

    workspace_mock.post(
        "/v1/workspaces/ws_01TESTWORKSPACE/channels/research-out/schema/check"
    ).mock(side_effect=handler)

    ws = client.workspace("ws")
    report = ws.channels.preview_schema_change(
        "research-out", {"type": "object"}
    )
    assert report["compatible"] is True
    # The recommendation explicitly calls out the lingering DLQ rows.
    assert "review" in report["recommendation"].lower()
