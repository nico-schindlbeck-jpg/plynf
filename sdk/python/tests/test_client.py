# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the top-level :class:`plinth.Plinth` facade."""

from __future__ import annotations

import httpx
import pytest
import respx

from plinth import (
    Plinth,
    PlinthError,
    Unauthorized,
    Workspace,
    WorkspaceNotFound,
)

from .conftest import (
    GATEWAY_URL,
    WORKSPACE_URL,
    error_envelope,
    make_workspace,
)

# ---------------------------------------------------------------------------
# Construction & lifecycle
# ---------------------------------------------------------------------------


def test_requires_api_key():
    with pytest.raises(ValueError, match="api_key"):
        Plinth(api_key="")


def test_close_is_idempotent_via_context_manager(
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    with Plinth(
        workspace_url=WORKSPACE_URL,
        gateway_url=GATEWAY_URL,
        api_key="test-key",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
    ) as client:
        assert client.tools is not None


# ---------------------------------------------------------------------------
# Workspace get-or-create
# ---------------------------------------------------------------------------


def test_workspace_get_or_create_creates_when_missing(
    client: Plinth,
    workspace_mock: respx.MockRouter,
):
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": []})
    )
    created = make_workspace(name="brand-new")
    create_route = workspace_mock.post("/v1/workspaces").mock(
        return_value=httpx.Response(201, json=created)
    )

    ws = client.workspace("brand-new")

    assert isinstance(ws, Workspace)
    assert ws.name == "brand-new"
    assert ws.id == created["id"]
    assert create_route.called


def test_workspace_get_or_create_returns_existing(
    client: Plinth,
    workspace_mock: respx.MockRouter,
):
    existing = make_workspace(ws_id="ws_OLDONE", name="research-task-1")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [existing]})
    )
    create_route = workspace_mock.post("/v1/workspaces").mock(
        return_value=httpx.Response(201, json=make_workspace())
    )

    ws = client.workspace("research-task-1")

    assert ws.id == "ws_OLDONE"
    # Critically — we did NOT issue a POST.
    assert not create_route.called


def test_workspace_get_or_create_picks_latest_on_dupes(
    client: Plinth,
    workspace_mock: respx.MockRouter,
):
    older = make_workspace(ws_id="ws_OLDER", name="dup")
    older["updated_at"] = "2020-01-01T00:00:00+00:00"
    newer = make_workspace(ws_id="ws_NEWER", name="dup")
    newer["updated_at"] = "2030-01-01T00:00:00+00:00"

    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [older, newer]})
    )

    ws = client.workspace("dup")

    assert ws.id == "ws_NEWER"


def test_get_workspace_by_id(client: Plinth, workspace_mock: respx.MockRouter):
    payload = make_workspace(ws_id="ws_BY_ID", name="known")
    workspace_mock.get("/v1/workspaces/ws_BY_ID").mock(
        return_value=httpx.Response(200, json=payload)
    )

    ws = client.get_workspace("ws_BY_ID")

    assert ws.name == "known"


def test_get_workspace_404_raises_typed_error(client: Plinth, workspace_mock: respx.MockRouter):
    workspace_mock.get("/v1/workspaces/ws_MISSING").mock(
        return_value=httpx.Response(404, json=error_envelope("WORKSPACE_NOT_FOUND", "no such ws"))
    )

    with pytest.raises(WorkspaceNotFound) as info:
        client.get_workspace("ws_MISSING")

    assert info.value.code == "WORKSPACE_NOT_FOUND"
    assert info.value.status_code == 404
    assert info.value.response is not None


def test_list_workspaces_returns_workspace_objects(
    client: Plinth, workspace_mock: respx.MockRouter
):
    a = make_workspace(ws_id="ws_A", name="a")
    b = make_workspace(ws_id="ws_B", name="b")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [a, b]})
    )

    out = client.list_workspaces()

    assert {w.id for w in out} == {"ws_A", "ws_B"}


def test_delete_workspace(client: Plinth, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete("/v1/workspaces/ws_BYE").mock(return_value=httpx.Response(204))

    client.delete_workspace("ws_BYE")

    assert route.called


# ---------------------------------------------------------------------------
# Auth + generic error mapping
# ---------------------------------------------------------------------------


def test_401_maps_to_unauthorized(client: Plinth, workspace_mock: respx.MockRouter):
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(401, json=error_envelope("UNAUTHORIZED", "nope"))
    )

    with pytest.raises(Unauthorized):
        client.list_workspaces()


def test_5xx_maps_to_base_plinth_error(client: Plinth, workspace_mock: respx.MockRouter):
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(500, json=error_envelope("INTERNAL_ERROR", "boom"))
    )

    with pytest.raises(PlinthError) as info:
        client.list_workspaces()

    assert info.value.code == "INTERNAL_ERROR"
    assert info.value.status_code == 500


def test_auth_header_is_sent(client: Plinth, workspace_mock: respx.MockRouter):
    captured: dict[str, str] = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"workspaces": []})

    workspace_mock.get("/v1/workspaces").mock(side_effect=handler)
    client.list_workspaces()

    assert captured["auth"] == "Bearer test-key"


# ---------------------------------------------------------------------------
# Token counting & cost estimation (delegated wrappers)
# ---------------------------------------------------------------------------


def test_count_tokens_is_deterministic(client: Plinth):
    assert client.count_tokens("hello world") == client.count_tokens("hello world")
    assert client.count_tokens("") == 0


def test_estimate_cost_uses_sonnet_pricing(client: Plinth):
    # 1M input tokens at $3 + 1M output at $15 = $18.
    cost = client.estimate_cost(1_000_000, 1_000_000)
    assert cost == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# HTTP error-mapping edge cases
# ---------------------------------------------------------------------------


def test_404_without_envelope_falls_back_to_hint(client: Plinth, workspace_mock: respx.MockRouter):
    """If the server returns a non-JSON 404 body, we still raise the hinted class."""
    workspace_mock.get("/v1/workspaces/ws_X").mock(
        return_value=httpx.Response(404, content=b"plain text 404")
    )

    with pytest.raises(WorkspaceNotFound) as info:
        client.get_workspace("ws_X")

    assert info.value.status_code == 404


def test_unknown_status_falls_back_to_plinth_error(
    client: Plinth, workspace_mock: respx.MockRouter
):
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(418, content=b"I'm a teapot")
    )

    with pytest.raises(PlinthError) as info:
        client.list_workspaces()

    # No envelope, so no code; just the base error.
    assert info.value.status_code == 418
