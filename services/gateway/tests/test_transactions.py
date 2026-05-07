# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the workflow transactions API + Saga engine."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
import respx
from httpx import Response

from plinth_gateway.models import CompensationSpec, TransactionCall
from plinth_gateway.transactions import (
    render_arguments,
    render_compensation_arguments,
)


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------


def _ws_tool(
    tool_id: str,
    *,
    endpoint: str | None = None,
    idempotent: bool = False,
    cache_ttl_seconds: int | None = None,
    side_effects: str = "write",
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "name": tool_id,
        "description": f"Mock tool {tool_id}",
        "transport": "http",
        "endpoint": endpoint or f"http://mcp.test/invoke/{tool_id}",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "idempotent": idempotent,
        "side_effects": side_effects,
        "cache_ttl_seconds": cache_ttl_seconds,
        "auth_method": "none",
        "auth_config": {},
    }


async def _register(client, tool: dict[str, Any]) -> None:
    r = await client.post("/v1/tools/register", json=tool)
    assert r.status_code == 201, r.text


@pytest_asyncio.fixture
async def github_slack_tools(client) -> None:
    """Register two mock tools used by the bulk of the tests."""
    await _register(
        client,
        _ws_tool("github.create_issue", endpoint="http://mcp.test/invoke/github.create_issue"),
    )
    await _register(
        client,
        _ws_tool("github.update_issue", endpoint="http://mcp.test/invoke/github.update_issue"),
    )
    await _register(
        client,
        _ws_tool("slack.post_message", endpoint="http://mcp.test/invoke/slack.post_message"),
    )


# ---------------------------------------------------------------------------
# 1) Pure unit tests for argument templating
# ---------------------------------------------------------------------------


def test_render_whole_string_preserves_int_type() -> None:
    """A whole-string placeholder returns the raw value, not a string."""
    prior = [
        TransactionCall(
            id="txc_1",
            tx_id="tx_1",
            seq=0,
            tool_id="github.create_issue",
            arguments={},
            status="committed",
            result={"number": 42},
        )
    ]
    out = render_arguments({"issue_number": "{result.number}"}, prior)
    assert out == {"issue_number": 42}
    assert isinstance(out["issue_number"], int)


def test_render_seq_indexed_placeholder() -> None:
    prior = [
        TransactionCall(
            id="txc_1",
            tx_id="tx_1",
            seq=0,
            tool_id="github.create_issue",
            arguments={},
            status="committed",
            result={"number": 7, "html_url": "https://example.com/issues/7"},
        )
    ]
    out = render_arguments(
        {"text": "Issue created: {seq.0.result.html_url}"},
        prior,
    )
    assert out == {"text": "Issue created: https://example.com/issues/7"}


def test_render_nested_dict_and_list() -> None:
    prior = [
        TransactionCall(
            id="txc_1",
            tx_id="tx_1",
            seq=0,
            tool_id="x",
            arguments={},
            status="committed",
            result={"id": "abc", "labels": ["a", "b"]},
        )
    ]
    out = render_arguments(
        {"meta": {"id": "{result.id}", "tags": ["{result.id}"]}},
        prior,
    )
    assert out == {"meta": {"id": "abc", "tags": ["abc"]}}


def test_render_missing_seq_raises() -> None:
    from plinth_gateway.exceptions import TransactionRenderError

    with pytest.raises(TransactionRenderError):
        render_arguments({"x": "{seq.99.result.foo}"}, [])


def test_render_missing_field_raises() -> None:
    from plinth_gateway.exceptions import TransactionRenderError

    prior = [
        TransactionCall(
            id="txc_1",
            tx_id="tx_1",
            seq=0,
            tool_id="x",
            arguments={},
            status="committed",
            result={"number": 1},
        )
    ]
    with pytest.raises(TransactionRenderError):
        render_arguments({"x": "{seq.0.result.does_not_exist}"}, prior)


def test_render_compensation_args_uses_forward_result() -> None:
    spec = CompensationSpec(
        tool_id="github.update_issue",
        arguments_template={
            "issue_number": "{result.number}",
            "state": "closed",
        },
    )
    out = render_compensation_arguments(spec, {"number": 99}, prior_calls=[])
    assert out == {"issue_number": 99, "state": "closed"}


def test_render_passthrough_with_no_placeholders() -> None:
    out = render_arguments({"a": 1, "b": "hello", "c": True}, prior_calls=[])
    assert out == {"a": 1, "b": "hello", "c": True}


def test_render_no_placeholder_braces_in_strings() -> None:
    """Strings without our placeholder shape are left untouched."""
    out = render_arguments({"x": "literal {1} text", "y": "{not.our.shape}"}, [])
    assert out == {"x": "literal {1} text", "y": "{not.our.shape}"}


# ---------------------------------------------------------------------------
# 2) HTTP API — basic CRUD + lifecycle
# ---------------------------------------------------------------------------


async def test_create_transaction_returns_pending(client) -> None:
    r = await client.post(
        "/v1/transactions",
        json={
            "workspace_id": "ws_a",
            "agent_id": "agt_a",
            "metadata": {"reason": "demo"},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["id"].startswith("tx_")
    assert body["workspace_id"] == "ws_a"
    assert body["agent_id"] == "agt_a"
    assert body["metadata"] == {"reason": "demo"}
    assert body["calls"] == []


async def test_add_call_assigns_monotonic_seq(client, github_slack_tools) -> None:
    r = await client.post("/v1/transactions", json={"workspace_id": "ws_a"})
    tx = r.json()
    tx_id = tx["id"]

    r = await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "github.create_issue",
            "arguments": {"repo": "owner/name", "title": "First"},
        },
    )
    assert r.status_code == 201
    c1 = r.json()
    assert c1["seq"] == 0
    assert c1["status"] == "pending"
    assert c1["id"].startswith("txc_")

    r = await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "slack.post_message",
            "arguments": {"channel": "C123", "text": "hi"},
        },
    )
    c2 = r.json()
    assert c2["seq"] == 1


async def test_get_transaction_includes_calls(client, github_slack_tools) -> None:
    r = await client.post("/v1/transactions", json={})
    tx_id = r.json()["id"]

    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {}},
    )

    r = await client.get(f"/v1/transactions/{tx_id}")
    body = r.json()
    assert r.status_code == 200
    assert len(body["calls"]) == 1
    assert body["calls"][0]["tool_id"] == "github.create_issue"


async def test_get_unknown_transaction_404(client) -> None:
    r = await client.get("/v1/transactions/tx_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


async def test_list_transactions_filters(client, github_slack_tools) -> None:
    a = (await client.post("/v1/transactions", json={"workspace_id": "ws_a"})).json()
    b = (await client.post("/v1/transactions", json={"workspace_id": "ws_b"})).json()

    r = await client.get("/v1/transactions", params={"workspace_id": "ws_a"})
    items = r.json()["transactions"]
    ids = {t["id"] for t in items}
    assert a["id"] in ids
    assert b["id"] not in ids

    # Status filter
    r = await client.get("/v1/transactions", params={"status": "pending"})
    assert r.status_code == 200
    assert all(t["status"] == "pending" for t in r.json()["transactions"])


async def test_delete_pending_transaction(client) -> None:
    r = await client.post("/v1/transactions", json={})
    tx_id = r.json()["id"]
    r = await client.delete(f"/v1/transactions/{tx_id}")
    assert r.status_code == 204
    r = await client.get(f"/v1/transactions/{tx_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3) Commit semantics — happy path
# ---------------------------------------------------------------------------


async def test_commit_executes_calls_in_seq_order(
    client, github_slack_tools
) -> None:
    """All calls succeed → status committed; backends invoked in seq order."""
    r = await client.post("/v1/transactions", json={"workspace_id": "ws_a"})
    tx_id = r.json()["id"]

    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "github.create_issue",
            "arguments": {"repo": "owner/name", "title": "X"},
        },
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "slack.post_message",
            "arguments": {"channel": "C", "text": "hi"},
        },
    )

    call_order: list[str] = []

    def _record(name: str):
        def _handler(request):
            call_order.append(name)
            if name == "github.create_issue":
                return Response(200, json={"number": 42, "html_url": "https://x/42"})
            return Response(200, json={"ts": "1"})

        return _handler

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            side_effect=_record("github.create_issue")
        )
        mock.post("http://mcp.test/invoke/slack.post_message").mock(
            side_effect=_record("slack.post_message")
        )
        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "committed"
    assert body["compensations_run"] == 0
    assert len(body["calls"]) == 2
    # In-order execution
    assert call_order == ["github.create_issue", "slack.post_message"]
    # Each call has a recorded result
    assert body["calls"][0]["result"]["number"] == 42
    assert body["calls"][0]["status"] == "committed"


async def test_commit_renders_seq_template(client, github_slack_tools) -> None:
    """``{seq.0.result.html_url}`` is substituted before the second call."""
    r = await client.post("/v1/transactions", json={})
    tx_id = r.json()["id"]

    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "github.create_issue",
            "arguments": {"repo": "owner/name", "title": "X"},
        },
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "slack.post_message",
            "arguments": {
                "channel": "C123",
                "text": "Issue created: {seq.0.result.html_url}",
            },
        },
    )

    seen_slack_body: dict[str, Any] = {}

    def _slack_handler(request):
        import json as _json

        seen_slack_body.update(_json.loads(request.content))
        return Response(200, json={"ts": "1"})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 7, "html_url": "https://gh/7"})
        )
        mock.post("http://mcp.test/invoke/slack.post_message").mock(
            side_effect=_slack_handler
        )
        r = await client.post(f"/v1/transactions/{tx_id}/commit")
        assert r.status_code == 200

    assert seen_slack_body["text"] == "Issue created: https://gh/7"


# ---------------------------------------------------------------------------
# 4) Compensation cascade
# ---------------------------------------------------------------------------


async def test_mid_failure_compensates_in_reverse(
    client, github_slack_tools
) -> None:
    r = await client.post("/v1/transactions", json={})
    tx_id = r.json()["id"]

    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "github.create_issue",
            "arguments": {"repo": "owner/name", "title": "X"},
            "compensation": {
                "tool_id": "github.update_issue",
                "arguments_template": {
                    "repo": "owner/name",
                    "issue_number": "{result.number}",
                    "state": "closed",
                },
            },
        },
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "slack.post_message",
            "arguments": {"channel": "C", "text": "hi"},
        },
    )

    update_calls: list[dict[str, Any]] = []

    def _update_handler(request):
        import json as _json

        update_calls.append(_json.loads(request.content))
        return Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 11})
        )
        mock.post("http://mcp.test/invoke/slack.post_message").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        update_route = mock.post("http://mcp.test/invoke/github.update_issue").mock(
            side_effect=_update_handler
        )

        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rolled_back"
    assert body["compensations_run"] == 1
    assert update_route.called
    # The compensation rendered the issue_number from the forward result.
    assert update_calls[0] == {
        "repo": "owner/name",
        "issue_number": 11,
        "state": "closed",
    }

    # Slack call ended in failed; github call is compensated.
    statuses = {c["tool_id"]: c["status"] for c in body["calls"]}
    assert statuses["github.create_issue"] == "compensated"
    assert statuses["slack.post_message"] == "failed"


async def test_compensation_runs_in_reverse_order(client) -> None:
    """Two compensations fire in reverse seq order on a third-call failure."""
    await _register(client, _ws_tool("svc.a", endpoint="http://mcp.test/a"))
    await _register(client, _ws_tool("svc.b", endpoint="http://mcp.test/b"))
    await _register(client, _ws_tool("svc.c", endpoint="http://mcp.test/c"))
    await _register(client, _ws_tool("svc.undo_a", endpoint="http://mcp.test/undo_a"))
    await _register(client, _ws_tool("svc.undo_b", endpoint="http://mcp.test/undo_b"))

    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    for tool_id, undo in [("svc.a", "svc.undo_a"), ("svc.b", "svc.undo_b")]:
        await client.post(
            f"/v1/transactions/{tx_id}/calls",
            json={
                "tool_id": tool_id,
                "arguments": {},
                "compensation": {"tool_id": undo, "arguments_template": {}},
            },
        )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.c", "arguments": {}},
    )

    order: list[str] = []

    def _handler(name, status=200, payload=None, raises=False):
        def _h(request):
            order.append(name)
            if raises:
                return Response(500, json={"error": "boom"})
            return Response(status, json=payload or {"ok": True})

        return _h

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/a").mock(side_effect=_handler("a"))
        mock.post("http://mcp.test/b").mock(side_effect=_handler("b"))
        mock.post("http://mcp.test/c").mock(side_effect=_handler("c", raises=True))
        mock.post("http://mcp.test/undo_a").mock(side_effect=_handler("undo_a"))
        mock.post("http://mcp.test/undo_b").mock(side_effect=_handler("undo_b"))

        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.status_code == 200
    assert r.json()["status"] == "rolled_back"
    assert r.json()["compensations_run"] == 2
    # Forward then reverse compensation order.
    assert order == ["a", "b", "c", "undo_b", "undo_a"]


async def test_compensation_failure_logged_partial_count(client) -> None:
    """A failing compensation tool is best-effort — txn still rolls back."""
    await _register(client, _ws_tool("svc.a", endpoint="http://mcp.test/a"))
    await _register(client, _ws_tool("svc.b", endpoint="http://mcp.test/b"))
    await _register(client, _ws_tool("svc.undo_a", endpoint="http://mcp.test/undo_a"))
    await _register(client, _ws_tool("svc.undo_b", endpoint="http://mcp.test/undo_b"))

    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "svc.a",
            "arguments": {},
            "compensation": {"tool_id": "svc.undo_a", "arguments_template": {}},
        },
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "svc.b",
            "arguments": {},
            "compensation": {"tool_id": "svc.undo_b", "arguments_template": {}},
        },
    )
    # Force the third call to fail to trigger compensation.
    await _register(client, _ws_tool("svc.boom", endpoint="http://mcp.test/boom"))
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.boom", "arguments": {}},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/a").mock(return_value=Response(200, json={}))
        mock.post("http://mcp.test/b").mock(return_value=Response(200, json={}))
        mock.post("http://mcp.test/boom").mock(return_value=Response(500, json={"error": "x"}))
        # undo_b succeeds, undo_a fails — compensation must continue
        mock.post("http://mcp.test/undo_b").mock(return_value=Response(200, json={}))
        mock.post("http://mcp.test/undo_a").mock(return_value=Response(500, json={"error": "rip"}))

        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rolled_back"
    # Only one compensation actually succeeded.
    assert body["compensations_run"] == 1
    # The failed compensation's call has an error stamped.
    by_tool = {c["tool_id"]: c for c in body["calls"]}
    assert by_tool["svc.a"]["status"] == "compensating"
    assert by_tool["svc.a"]["error"] is not None
    assert "compensation failed" in by_tool["svc.a"]["error"]
    assert by_tool["svc.b"]["status"] == "compensated"


async def test_calls_without_compensation_are_skipped_on_rollback(
    client,
) -> None:
    """A successful call without a compensation_spec is left as-is."""
    await _register(client, _ws_tool("svc.a", endpoint="http://mcp.test/a"))
    await _register(client, _ws_tool("svc.b", endpoint="http://mcp.test/b"))
    await _register(client, _ws_tool("svc.boom", endpoint="http://mcp.test/boom"))

    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.a", "arguments": {}},  # no comp
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.b", "arguments": {}},  # no comp
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.boom", "arguments": {}},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/a").mock(return_value=Response(200, json={}))
        mock.post("http://mcp.test/b").mock(return_value=Response(200, json={}))
        mock.post("http://mcp.test/boom").mock(return_value=Response(500, json={"e": "x"}))

        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    body = r.json()
    assert body["status"] == "rolled_back"
    assert body["compensations_run"] == 0
    by_tool = {c["tool_id"]: c["status"] for c in body["calls"]}
    assert by_tool == {"svc.a": "committed", "svc.b": "committed", "svc.boom": "failed"}


# ---------------------------------------------------------------------------
# 5) Idempotency
# ---------------------------------------------------------------------------


async def test_commit_already_committed_is_idempotent(client, github_slack_tools) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {"title": "X"}},
    )

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 7})
        )
        r1 = await client.post(f"/v1/transactions/{tx_id}/commit")
        assert r1.status_code == 200
        assert r1.json()["status"] == "committed"
        first_calls = route.call_count

        # Second commit returns same shape but does NOT re-invoke.
        r2 = await client.post(f"/v1/transactions/{tx_id}/commit")
        assert r2.status_code == 200
        assert r2.json()["status"] == "committed"
        assert route.call_count == first_calls


async def test_commit_already_rolled_back_is_idempotent(client) -> None:
    await _register(client, _ws_tool("svc.boom", endpoint="http://mcp.test/boom"))
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.boom", "arguments": {}},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/boom").mock(return_value=Response(500, json={"e": "x"}))
        r1 = await client.post(f"/v1/transactions/{tx_id}/commit")
        assert r1.json()["status"] == "rolled_back"

        # Second commit short-circuits — no extra backend calls.
        r2 = await client.post(f"/v1/transactions/{tx_id}/commit")
        assert r2.json()["status"] == "rolled_back"


# ---------------------------------------------------------------------------
# 6) Manual rollback paths
# ---------------------------------------------------------------------------


async def test_rollback_pending_just_marks_rolled_back(client) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    r = await client.post(f"/v1/transactions/{tx_id}/rollback")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rolled_back"
    assert body["compensations_run"] == 0


async def test_rollback_committed_is_rejected(client, github_slack_tools) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {"title": "X"}},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 1})
        )
        r = await client.post(f"/v1/transactions/{tx_id}/commit")
        assert r.json()["status"] == "committed"

    r = await client.post(f"/v1/transactions/{tx_id}/rollback")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRANSACTION_INVALID_STATUS"


async def test_delete_committed_transaction_rejected(client, github_slack_tools) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {}},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 1})
        )
        await client.post(f"/v1/transactions/{tx_id}/commit")

    r = await client.delete(f"/v1/transactions/{tx_id}")
    assert r.status_code == 409


async def test_delete_rolled_back_transaction_succeeds(client) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(f"/v1/transactions/{tx_id}/rollback")
    r = await client.delete(f"/v1/transactions/{tx_id}")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# 7) Add-call after commit forbidden
# ---------------------------------------------------------------------------


async def test_add_call_to_committed_tx_rejected(client, github_slack_tools) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {}},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 1})
        )
        await client.post(f"/v1/transactions/{tx_id}/commit")

    r = await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {}},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRANSACTION_INVALID_STATUS"


# ---------------------------------------------------------------------------
# 8) Audit + cache integration
# ---------------------------------------------------------------------------


async def test_each_call_emits_audit_event(client, github_slack_tools) -> None:
    tx_id = (await client.post("/v1/transactions", json={"workspace_id": "ws_a"})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {"title": "X"}},
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "slack.post_message", "arguments": {"channel": "C", "text": "hi"}},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 1})
        )
        mock.post("http://mcp.test/invoke/slack.post_message").mock(
            return_value=Response(200, json={"ts": "1"})
        )
        await client.post(f"/v1/transactions/{tx_id}/commit")

    r = await client.get("/v1/audit", params={"workspace_id": "ws_a"})
    events = r.json()["events"]
    tool_ids = {e["tool_id"] for e in events}
    assert "github.create_issue" in tool_ids
    assert "slack.post_message" in tool_ids


async def test_idempotent_tool_uses_cache_on_commit(client) -> None:
    """A cacheable tool consulted twice in one tx hits cache the second time."""
    await _register(
        client,
        _ws_tool(
            "fs.read_idem",
            endpoint="http://mcp.test/fs.read_idem",
            idempotent=True,
            cache_ttl_seconds=300,
        ),
    )

    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "fs.read_idem", "arguments": {"path": "x"}},
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "fs.read_idem", "arguments": {"path": "x"}},
    )

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/fs.read_idem").mock(
            return_value=Response(200, json={"content": "abc"})
        )
        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.json()["status"] == "committed"
    # Same args twice → exactly one backend hit.
    assert route.call_count == 1


async def test_audit_records_compensation_call(client) -> None:
    """The compensating call also appears in the audit log."""
    await _register(client, _ws_tool("svc.a", endpoint="http://mcp.test/a"))
    await _register(client, _ws_tool("svc.undo_a", endpoint="http://mcp.test/undo_a"))
    await _register(client, _ws_tool("svc.boom", endpoint="http://mcp.test/boom"))

    tx_id = (await client.post("/v1/transactions", json={"workspace_id": "ws_x"})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "svc.a",
            "arguments": {},
            "compensation": {"tool_id": "svc.undo_a", "arguments_template": {}},
        },
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.boom", "arguments": {}},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/a").mock(return_value=Response(200, json={}))
        mock.post("http://mcp.test/boom").mock(return_value=Response(500, json={"e": "x"}))
        mock.post("http://mcp.test/undo_a").mock(return_value=Response(200, json={}))

        await client.post(f"/v1/transactions/{tx_id}/commit")

    r = await client.get("/v1/audit", params={"workspace_id": "ws_x"})
    tool_ids = [e["tool_id"] for e in r.json()["events"]]
    assert "svc.undo_a" in tool_ids


# ---------------------------------------------------------------------------
# 9) Tenant isolation
# ---------------------------------------------------------------------------


async def test_tenant_isolation_under_strict_auth(tmp_path) -> None:
    """A tx in tenant A is invisible to a request in tenant B."""
    import jwt
    from httpx import ASGITransport, AsyncClient

    from plinth_gateway.api import create_app
    from plinth_gateway.settings import Settings

    secret = "x" * 32
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        auth_mode="verify_local",
        identity_jwt_secret=secret,
        rate_limits_enabled=False,
    )
    settings.ensure_data_dir()
    app = create_app(settings)

    def _token(tenant: str, agent: str = "agt_x") -> str:
        return jwt.encode(
            {
                "sub": agent,
                "iss": settings.identity_url,
                "aud": settings.jwt_audience,
                "iat": 0,
                "exp": 9999999999,
                "jti": "j",
                "agent_id": agent,
                "tenant_id": tenant,
                "scopes": ["*"],
            },
            secret,
            algorithm="HS256",
        )

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/v1/transactions",
                json={"workspace_id": "ws_a"},
                headers={"Authorization": f"Bearer {_token('tenant_a')}"},
            )
            assert r.status_code == 201
            tx_id = r.json()["id"]

            # Tenant B can't see it.
            r = await c.get(
                f"/v1/transactions/{tx_id}",
                headers={"Authorization": f"Bearer {_token('tenant_b')}"},
            )
            assert r.status_code == 404

            # Tenant A still can.
            r = await c.get(
                f"/v1/transactions/{tx_id}",
                headers={"Authorization": f"Bearer {_token('tenant_a')}"},
            )
            assert r.status_code == 200

            # Listing is also scoped.
            r = await c.get(
                "/v1/transactions",
                headers={"Authorization": f"Bearer {_token('tenant_b')}"},
            )
            assert tx_id not in {t["id"] for t in r.json()["transactions"]}


# ---------------------------------------------------------------------------
# 10) Render-error path inside commit
# ---------------------------------------------------------------------------


async def test_render_error_aborts_with_failed_call(client, github_slack_tools) -> None:
    """Bad placeholder before the second call → second call fails to render."""
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {"title": "X"}},
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "slack.post_message",
            "arguments": {"channel": "C", "text": "{seq.0.result.does_not_exist}"},
        },
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 7})
        )
        slack_route = mock.post("http://mcp.test/invoke/slack.post_message").mock(
            return_value=Response(200, json={})
        )
        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.status_code == 200
    body = r.json()
    # First call committed; second failed during render → transaction rolled back
    assert body["status"] == "rolled_back"
    by_tool = {c["tool_id"]: c["status"] for c in body["calls"]}
    assert by_tool["github.create_issue"] == "committed"
    assert by_tool["slack.post_message"] == "failed"
    # Slack backend was never called.
    assert not slack_route.called


# ---------------------------------------------------------------------------
# 11) Rollback (manual) on a partial commit-state
# ---------------------------------------------------------------------------


async def test_rollback_committed_call_compensates(client) -> None:
    """If a transaction is parked in 'committing' mid-flight, manual rollback
    runs the compensations for already-committed calls."""
    await _register(client, _ws_tool("svc.a", endpoint="http://mcp.test/a"))
    await _register(client, _ws_tool("svc.undo_a", endpoint="http://mcp.test/undo_a"))

    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "svc.a",
            "arguments": {},
            "compensation": {"tool_id": "svc.undo_a", "arguments_template": {}},
        },
    )

    # Manually drive the call through to "committed" status so we can hit
    # the rollback path on a half-finished tx.
    from plinth_gateway.transactions import TransactionStore

    db = client._transport.app.state.db  # type: ignore[attr-defined]
    store = TransactionStore(db)
    tx = await store.get(tx_id)
    await store.update_call(
        tx.calls[0].id,
        status="committed",
        result={"id": "abc"},
    )
    await store.update_status(tx.id, "committing")

    with respx.mock(assert_all_called=False) as mock:
        undo_route = mock.post("http://mcp.test/undo_a").mock(
            return_value=Response(200, json={"ok": True})
        )
        r = await client.post(f"/v1/transactions/{tx_id}/rollback")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rolled_back"
    assert body["compensations_run"] == 1
    assert undo_route.called


# ---------------------------------------------------------------------------
# 12) Rate limits & cost caps still apply per call
# ---------------------------------------------------------------------------


async def test_rate_limit_applies_inside_transaction(
    app_and_client, github_slack_tools
) -> None:
    """Per-call rate limiting + cost caps still gate transaction commits."""
    app, client = app_and_client
    # Tighten down to 1 rpm + burst=1 so we trip on the 2nd call.
    await client.post("/v1/limits/agt_low", json={"rpm": 1, "burst": 1})

    tx_id = (await client.post(
        "/v1/transactions", json={"agent_id": "agt_low"}
    )).json()["id"]

    # Two cacheable calls so the cache doesn't save us.
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "github.create_issue", "arguments": {"title": "1"}},
    )
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "slack.post_message", "arguments": {"channel": "C", "text": "x"}},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 1})
        )
        mock.post("http://mcp.test/invoke/slack.post_message").mock(
            return_value=Response(200, json={})
        )
        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    body = r.json()
    # Second call should have been rate-limited; transaction rolled back.
    assert body["status"] == "rolled_back"
    by_tool = {c["tool_id"]: c["status"] for c in body["calls"]}
    assert by_tool["github.create_issue"] == "committed"
    assert by_tool["slack.post_message"] == "failed"


# ---------------------------------------------------------------------------
# 13) Validation surface
# ---------------------------------------------------------------------------


async def test_unknown_tool_in_call_fails_during_commit(client) -> None:
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    # Adding the call doesn't validate (we only need tool registration at
    # commit time — and the tx may target tools that get registered later).
    r = await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "does.not.exist", "arguments": {}},
    )
    assert r.status_code == 201

    r = await client.post(f"/v1/transactions/{tx_id}/commit")
    body = r.json()
    assert body["status"] == "rolled_back"
    assert body["calls"][0]["status"] == "failed"
    assert body["calls"][0]["error"]


async def test_commit_unknown_transaction(client) -> None:
    r = await client.post("/v1/transactions/tx_nope/commit")
    assert r.status_code == 404


async def test_rollback_unknown_transaction(client) -> None:
    r = await client.post("/v1/transactions/tx_nope/rollback")
    assert r.status_code == 404


async def test_add_call_to_unknown_transaction(client) -> None:
    r = await client.post(
        "/v1/transactions/tx_nope/calls",
        json={"tool_id": "github.create_issue", "arguments": {}},
    )
    assert r.status_code == 404


async def test_commit_with_no_calls(client) -> None:
    """An empty pending transaction commits trivially with zero calls."""
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    r = await client.post(f"/v1/transactions/{tx_id}/commit")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "committed"
    assert body["calls"] == []
    assert body["compensations_run"] == 0


# ---------------------------------------------------------------------------
# 14) Compensation arguments via {seq.N.result.field}
# ---------------------------------------------------------------------------


async def test_compensation_can_reference_seq_indexed_result(
    client, github_slack_tools
) -> None:
    """Compensation templates may pull from any prior call, not just self."""
    tx_id = (await client.post("/v1/transactions", json={})).json()["id"]
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={
            "tool_id": "github.create_issue",
            "arguments": {"title": "first"},
            "compensation": {
                "tool_id": "github.update_issue",
                "arguments_template": {
                    "issue_number": "{seq.0.result.number}",
                    "state": "closed",
                },
            },
        },
    )
    # Force failure with a doomed second call.
    await _register(client, _ws_tool("svc.fail", endpoint="http://mcp.test/fail"))
    await client.post(
        f"/v1/transactions/{tx_id}/calls",
        json={"tool_id": "svc.fail", "arguments": {}},
    )

    seen_update: dict[str, Any] = {}

    def _record(request):
        import json as _j

        seen_update.update(_j.loads(request.content))
        return Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/github.create_issue").mock(
            return_value=Response(200, json={"number": 99})
        )
        mock.post("http://mcp.test/fail").mock(
            return_value=Response(500, json={"e": "x"})
        )
        mock.post("http://mcp.test/invoke/github.update_issue").mock(
            side_effect=_record
        )

        r = await client.post(f"/v1/transactions/{tx_id}/commit")

    assert r.json()["status"] == "rolled_back"
    assert seen_update == {"issue_number": 99, "state": "closed"}
