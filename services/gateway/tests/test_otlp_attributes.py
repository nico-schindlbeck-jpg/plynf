# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Verify the canonical OTLP attribute schema documented in
``docs/observability.md``.

These tests pin the snake_case → dotted name mapping. Any future change to
``_build_attributes`` that breaks the documented schema must update both
the doc and these tests in the same PR.
"""

from __future__ import annotations

from plinth_gateway.otlp_emitter import _build_attributes


def test_common_service_name_always_present():
    attrs = _build_attributes({}, "plinth-gateway")
    assert attrs["service.name"] == "plinth-gateway"


def test_service_version_propagates():
    attrs = _build_attributes(
        {}, "plinth-gateway", service_version="1.0.0"
    )
    assert attrs["service.version"] == "1.0.0"


def test_region_id_propagates_when_set():
    attrs = _build_attributes(
        {}, "plinth-gateway", region_id="us-west-2"
    )
    assert attrs["region.id"] == "us-west-2"


def test_region_id_omitted_when_unset():
    attrs = _build_attributes({}, "plinth-gateway")
    assert "region.id" not in attrs


def test_per_tool_attributes_use_dotted_names():
    event = {
        "tool_id": "weather.lookup",
        "cached": True,
        "duration_ms": 42,
        "cost_estimate_usd": 0.0123,
    }
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["tool.id"] == "weather.lookup"
    assert attrs["tool.cached"] is True
    assert attrs["tool.duration_ms"] == 42
    assert attrs["tool.cost_usd"] == 0.0123


def test_per_tenant_and_per_workspace_attributes():
    event = {
        "tenant_id": "acme",
        "agent_id": "agent_1",
        "workspace_id": "ws_a",
    }
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["tenant.id"] == "acme"
    assert attrs["agent.id"] == "agent_1"
    assert attrs["workspace.id"] == "ws_a"


def test_per_workflow_snake_case_input():
    """Input field ``workflow_id`` maps to ``workflow.id``."""
    event = {"workflow_id": "wf_42", "workflow_step": "fetch"}
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["workflow.id"] == "wf_42"
    assert attrs["workflow.step"] == "fetch"


def test_per_workflow_dotted_input_already_canonical():
    """Input field ``workflow.id`` flows through unchanged."""
    event = {"workflow.id": "wf_42", "workflow.step": "fetch"}
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["workflow.id"] == "wf_42"
    assert attrs["workflow.step"] == "fetch"


def test_arguments_and_result_hashes_dotted():
    event = {
        "arguments_hash": "deadbeef",
        "arguments_preview": "abc",
        "result_hash": "feedface",
    }
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["arguments.hash"] == "deadbeef"
    assert attrs["arguments.preview"] == "abc"
    assert attrs["result.hash"] == "feedface"


def test_error_message_dotted():
    event = {"error": "boom"}
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["error.message"] == "boom"


def test_audit_id_dotted():
    event = {"id": "evt_1"}
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["audit.id"] == "evt_1"
