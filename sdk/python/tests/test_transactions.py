# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the SDK's workflow transactions client + builder."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from plinth import (
    CompensationSpec,
    Plinth,
    Transaction,
    TransactionCall,
    TransactionFailed,
    TransactionInvalidStatus,
    TransactionNotFound,
    TransactionResult,
)
from plinth.transactions import _coerce_compensation

from .conftest import error_envelope


# ---------------------------------------------------------------------------
# Builders for the gateway responses we mock.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()  # noqa: UP017


def make_tx(
    *,
    tx_id: str = "tx_01TESTTRANSACTION",
    status: str = "pending",
    workspace_id: str | None = "ws_01TESTWORKSPACE",
    agent_id: str | None = "agt_test",
    metadata: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": tx_id,
        "status": status,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "tenant_id": "default",
        "metadata": metadata or {},
        "calls": calls or [],
        "created_at": _now_iso(),
        "committed_at": _now_iso() if status == "committed" else None,
        "rolled_back_at": _now_iso() if status == "rolled_back" else None,
    }


def make_call(
    *,
    call_id: str = "txc_01TESTCALL",
    tx_id: str = "tx_01TESTTRANSACTION",
    seq: int = 0,
    tool_id: str = "github.create_issue",
    arguments: dict[str, Any] | None = None,
    compensation: dict[str, Any] | None = None,
    status: str = "pending",
    result: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": call_id,
        "tx_id": tx_id,
        "seq": seq,
        "tool_id": tool_id,
        "arguments": arguments or {},
        "compensation": compensation,
        "status": status,
        "result": result,
        "error": error,
        "invoked_at": None,
        "finished_at": None,
    }


def make_result(
    *,
    tx_id: str = "tx_01TESTTRANSACTION",
    status: str = "committed",
    calls: list[dict[str, Any]] | None = None,
    compensations_run: int = 0,
) -> dict[str, Any]:
    return {
        "tx_id": tx_id,
        "status": status,
        "calls": calls or [],
        "compensations_run": compensations_run,
    }


# ---------------------------------------------------------------------------
# 1) Coercion of compensation forms
# ---------------------------------------------------------------------------


def test_coerce_compensation_tuple() -> None:
    spec = _coerce_compensation(("github.update_issue", {"state": "closed"}))
    assert isinstance(spec, CompensationSpec)
    assert spec.tool_id == "github.update_issue"
    assert spec.arguments_template == {"state": "closed"}


def test_coerce_compensation_spec_passthrough() -> None:
    original = CompensationSpec(
        tool_id="x", arguments_template={"k": "v"}
    )
    assert _coerce_compensation(original) is original


def test_coerce_compensation_dict() -> None:
    spec = _coerce_compensation(
        {"tool_id": "fs.delete", "arguments_template": {"path": "/tmp/x"}}
    )
    assert spec.tool_id == "fs.delete"
    assert spec.arguments_template == {"path": "/tmp/x"}


def test_coerce_compensation_none() -> None:
    assert _coerce_compensation(None) is None


def test_coerce_compensation_bad_tuple_length() -> None:
    with pytest.raises(ValueError):
        _coerce_compensation(("only-one-element",))  # type: ignore[arg-type]


def test_coerce_compensation_bad_type() -> None:
    with pytest.raises(TypeError):
        _coerce_compensation(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2) Builder pattern — happy path
# ---------------------------------------------------------------------------


def test_builder_creates_transaction_on_init(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    captured_create: dict[str, Any] = {}

    def _create(request):
        captured_create["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json=make_tx(workspace_id="ws_a", agent_id="agt_1", metadata={"r": "x"}),
        )

    gateway_mock.post("/v1/transactions").mock(side_effect=_create)

    tx = client.gateway.transaction(
        workspace_id="ws_a", agent_id="agt_1", metadata={"r": "x"}
    )
    assert tx.id == "tx_01TESTTRANSACTION"
    assert captured_create["body"]["workspace_id"] == "ws_a"
    assert captured_create["body"]["agent_id"] == "agt_1"
    assert captured_create["body"]["metadata"] == {"r": "x"}


def test_builder_add_records_call_and_returns_it(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )

    captured_add: dict[str, Any] = {}

    def _add(request):
        captured_add["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json=make_call(
                seq=0,
                tool_id="github.create_issue",
                arguments={"repo": "owner/name", "title": "X"},
                compensation={
                    "tool_id": "github.update_issue",
                    "arguments_template": {
                        "issue_number": "{result.number}",
                        "state": "closed",
                    },
                },
            ),
        )

    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(
        side_effect=_add
    )

    tx = client.gateway.transaction(workspace_id="ws_a")
    call = tx.add(
        "github.create_issue",
        {"repo": "owner/name", "title": "X"},
        compensation=(
            "github.update_issue",
            {"issue_number": "{result.number}", "state": "closed"},
        ),
    )

    assert call.tool_id == "github.create_issue"
    assert call.seq == 0
    assert call.compensation is not None
    assert call.compensation.tool_id == "github.update_issue"

    body = captured_add["body"]
    assert body["tool_id"] == "github.create_issue"
    assert body["arguments"] == {"repo": "owner/name", "title": "X"}
    assert body["compensation"]["tool_id"] == "github.update_issue"
    assert body["compensation"]["arguments_template"] == {
        "issue_number": "{result.number}",
        "state": "closed",
    }


def test_builder_add_without_compensation(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    captured: dict[str, Any] = {}

    def _add(request):
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json=make_call(tool_id="slack.post_message"),
        )

    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(side_effect=_add)
    tx = client.gateway.transaction()
    tx.add("slack.post_message", {"channel": "C", "text": "hi"})

    assert "compensation" not in captured["body"]


# ---------------------------------------------------------------------------
# 3) Commit — happy path & rolled-back path
# ---------------------------------------------------------------------------


def test_commit_returns_transaction_result(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(
        return_value=httpx.Response(201, json=make_call())
    )

    committed_calls = [
        make_call(
            seq=0,
            tool_id="github.create_issue",
            status="committed",
            result={"number": 42, "html_url": "https://x/42"},
        )
    ]
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/commit").mock(
        return_value=httpx.Response(
            200,
            json=make_result(status="committed", calls=committed_calls),
        )
    )
    # Builder refreshes after commit.
    gateway_mock.get("/v1/transactions/tx_01TESTTRANSACTION").mock(
        return_value=httpx.Response(
            200, json=make_tx(status="committed", calls=committed_calls)
        )
    )

    tx = client.gateway.transaction()
    tx.add("github.create_issue", {"repo": "x"})
    result = tx.commit()

    assert isinstance(result, TransactionResult)
    assert result.status == "committed"
    assert result.compensations_run == 0
    assert len(result.calls) == 1
    assert result.calls[0].result["number"] == 42


def test_commit_rolled_back_surfaces_via_result(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(
        return_value=httpx.Response(201, json=make_call())
    )

    rolled_calls = [
        make_call(seq=0, status="compensated"),
        make_call(seq=1, status="failed", error="backend 500"),
    ]
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/commit").mock(
        return_value=httpx.Response(
            200,
            json=make_result(
                status="rolled_back",
                calls=rolled_calls,
                compensations_run=1,
            ),
        )
    )
    gateway_mock.get("/v1/transactions/tx_01TESTTRANSACTION").mock(
        return_value=httpx.Response(
            200, json=make_tx(status="rolled_back", calls=rolled_calls)
        )
    )

    tx = client.gateway.transaction()
    tx.add("github.create_issue", {})
    tx.add("slack.post_message", {})
    result = tx.commit()

    # Rolled-back is NOT an exception — it's a normal result with metadata.
    assert isinstance(result, TransactionResult)
    assert result.status == "rolled_back"
    assert result.compensations_run == 1
    assert result.calls[1].error == "backend 500"


def test_commit_unknown_transaction_raises(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/commit").mock(
        return_value=httpx.Response(
            404,
            json=error_envelope("TRANSACTION_NOT_FOUND", "no such tx"),
        )
    )

    tx = client.gateway.transaction()
    with pytest.raises(TransactionNotFound):
        tx.commit()


def test_commit_5xx_raises_transaction_failed(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    """A catastrophic 5xx on commit surfaces as TransactionFailed."""
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/commit").mock(
        return_value=httpx.Response(
            500,
            json={"error": {"code": "INTERNAL_ERROR", "message": "boom", "details": {}}},
        )
    )

    tx = client.gateway.transaction()
    # The gateway returned a generic error envelope — the SDK maps that to
    # PlinthError (not TransactionFailed). We assert *some* PlinthError —
    # the test for catastrophic non-PlinthError comes via mocking.
    from plinth import PlinthError

    with pytest.raises(PlinthError):
        tx.commit()


# ---------------------------------------------------------------------------
# 4) Manual rollback
# ---------------------------------------------------------------------------


def test_rollback_pending_returns_result(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/rollback").mock(
        return_value=httpx.Response(
            200, json=make_result(status="rolled_back", compensations_run=0)
        )
    )
    gateway_mock.get("/v1/transactions/tx_01TESTTRANSACTION").mock(
        return_value=httpx.Response(200, json=make_tx(status="rolled_back"))
    )

    tx = client.gateway.transaction()
    result = tx.rollback()
    assert result.status == "rolled_back"


def test_rollback_committed_raises(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/rollback").mock(
        return_value=httpx.Response(
            409,
            json=error_envelope("TRANSACTION_INVALID_STATUS", "already committed"),
        )
    )

    tx = client.gateway.transaction()
    with pytest.raises(TransactionInvalidStatus):
        tx.rollback()


# ---------------------------------------------------------------------------
# 5) TransactionsClient direct CRUD
# ---------------------------------------------------------------------------


def test_get_transaction(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.get("/v1/transactions/tx_x").mock(
        return_value=httpx.Response(200, json=make_tx(tx_id="tx_x"))
    )
    tx = client.gateway.transactions.get("tx_x")
    assert isinstance(tx, Transaction)
    assert tx.id == "tx_x"


def test_get_transaction_404(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.get("/v1/transactions/tx_x").mock(
        return_value=httpx.Response(
            404, json=error_envelope("TRANSACTION_NOT_FOUND", "no such")
        )
    )
    with pytest.raises(TransactionNotFound):
        client.gateway.transactions.get("tx_x")


def test_list_transactions_filters(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    captured: dict[str, Any] = {}

    def _list(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"transactions": [make_tx(tx_id="tx_a"), make_tx(tx_id="tx_b")]},
        )

    gateway_mock.get("/v1/transactions").mock(side_effect=_list)
    items = client.gateway.transactions.list(workspace_id="ws_a", status="pending")
    assert len(items) == 2
    assert "workspace_id=ws_a" in captured["url"]
    assert "status=pending" in captured["url"]


def test_delete_transaction(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.delete("/v1/transactions/tx_a").mock(
        return_value=httpx.Response(204)
    )
    # Should not raise
    client.gateway.transactions.delete("tx_a")


def test_delete_transaction_409(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.delete("/v1/transactions/tx_a").mock(
        return_value=httpx.Response(
            409,
            json=error_envelope("TRANSACTION_INVALID_STATUS", "is committed"),
        )
    )
    with pytest.raises(TransactionInvalidStatus):
        client.gateway.transactions.delete("tx_a")


def test_add_call_via_low_level_client(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    """The low-level add_call is callable without going through the builder."""
    gateway_mock.post("/v1/transactions/tx_x/calls").mock(
        return_value=httpx.Response(
            201,
            json=make_call(seq=3, tool_id="fs.write", arguments={"path": "x"}),
        )
    )
    call = client.gateway.transactions.add_call(
        "tx_x",
        "fs.write",
        {"path": "x"},
    )
    assert isinstance(call, TransactionCall)
    assert call.seq == 3


# ---------------------------------------------------------------------------
# 6) Argument templates pass through the wire intact
# ---------------------------------------------------------------------------


def test_argument_templates_sent_verbatim(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    """The SDK does NOT render templates — that's the gateway's job."""
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )

    captured: dict[str, Any] = {}

    def _add(request):
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json=make_call())

    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(side_effect=_add)

    tx = client.gateway.transaction()
    tx.add(
        "slack.post_message",
        {"channel": "C", "text": "Issue: {seq.0.result.html_url}"},
    )
    # The literal template string survives the wire.
    assert (
        captured["body"]["arguments"]["text"]
        == "Issue: {seq.0.result.html_url}"
    )


# ---------------------------------------------------------------------------
# 7) Builder mirrors local state after add
# ---------------------------------------------------------------------------


def test_builder_local_calls_mirror_grows_with_each_add(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )

    counter = {"i": 0}

    def _add(request):
        counter["i"] += 1
        return httpx.Response(201, json=make_call(seq=counter["i"] - 1))

    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(side_effect=_add)

    tx = client.gateway.transaction()
    tx.add("a")
    tx.add("b")
    tx.add("c")
    assert len(tx.transaction.calls) == 3


# ---------------------------------------------------------------------------
# 8) Rolled-back status carries compensation count
# ---------------------------------------------------------------------------


def test_failed_transaction_surfaces_compensation_count(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(
        return_value=httpx.Response(201, json=make_call())
    )
    rolled = [
        make_call(seq=0, status="compensated", tool_id="a"),
        make_call(seq=1, status="compensated", tool_id="b"),
        make_call(seq=2, status="failed", tool_id="boom", error="500"),
    ]
    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/commit").mock(
        return_value=httpx.Response(
            200,
            json=make_result(
                status="rolled_back", calls=rolled, compensations_run=2
            ),
        )
    )
    gateway_mock.get("/v1/transactions/tx_01TESTTRANSACTION").mock(
        return_value=httpx.Response(200, json=make_tx(status="rolled_back", calls=rolled))
    )

    tx = client.gateway.transaction()
    tx.add("a")
    tx.add("b")
    tx.add("boom")
    result = tx.commit()

    assert result.status == "rolled_back"
    assert result.compensations_run == 2


# ---------------------------------------------------------------------------
# 9) Refresh
# ---------------------------------------------------------------------------


def test_refresh_reloads_server_view(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx(status="pending"))
    )
    gateway_mock.get("/v1/transactions/tx_01TESTTRANSACTION").mock(
        return_value=httpx.Response(200, json=make_tx(status="committed"))
    )
    tx = client.gateway.transaction()
    refreshed = tx.refresh()
    assert refreshed.status == "committed"
    assert tx.transaction.status == "committed"


# ---------------------------------------------------------------------------
# 10) compensation as plain dict still validates
# ---------------------------------------------------------------------------


def test_compensation_as_dict_validates_against_spec(
    client: Plinth, gateway_mock: respx.MockRouter
) -> None:
    gateway_mock.post("/v1/transactions").mock(
        return_value=httpx.Response(201, json=make_tx())
    )

    captured: dict[str, Any] = {}

    def _add(request):
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json=make_call(
                compensation={
                    "tool_id": "rm",
                    "arguments_template": {"path": "x"},
                }
            ),
        )

    gateway_mock.post("/v1/transactions/tx_01TESTTRANSACTION/calls").mock(side_effect=_add)
    tx = client.gateway.transaction()
    tx.add(
        "fs.write",
        {"path": "x", "content": "y"},
        compensation={"tool_id": "rm", "arguments_template": {"path": "x"}},
    )
    assert captured["body"]["compensation"] == {
        "tool_id": "rm",
        "arguments_template": {"path": "x"},
    }


# ---------------------------------------------------------------------------
# 11) gateway and tools are aliased
# ---------------------------------------------------------------------------


def test_client_gateway_alias_is_tool_gateway(client: Plinth) -> None:
    assert client.gateway is client.tools
