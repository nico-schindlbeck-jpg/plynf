# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Connector registry + execution.

In production this dispatches into the existing MCP gateway / per-tool MCP
servers. For the MVP demo we also ship deterministic mock connectors so the
full pipeline (proxy → policy → savings → response) works without external
credentials.

A connector is identified by its registry name (``salesforce``, ``orders``,
...). A tool is identified by a name like ``get_lead``. The proxy maps the
LLM-reported ``tool_calls[].function.name`` against the registry; unknown
names pass through unchanged.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Tool-name → connector-name map. The LLM is told tools are flat (it sees
# "get_order", not "orders.get_order"), so we keep this lookup small.
TOOL_TO_CONNECTOR: dict[str, str] = {
    # Salesforce
    "get_lead": "salesforce",
    "list_leads": "salesforce",
    "get_opportunity": "salesforce",
    "get_account": "salesforce",
    "get_contact": "salesforce",
    # Order DB
    "get_order": "orders",
    "list_orders_by_customer": "orders",
    "get_customer": "orders",
    "search_orders": "orders",
    # Slack
    "get_channel_messages": "slack",
    "search_messages": "slack",
    "get_user_info": "slack",
}


@dataclass
class ConnectorCall:
    connector: str
    tool: str
    args: dict[str, Any]


class ConnectorRegistry:
    """Resolves and executes connector calls.

    Concrete handlers are registered via :meth:`register`. Each handler is a
    ``Callable[[ConnectorCall], dict | list]`` that returns the raw JSON-
    serialisable response. The MVP ships with mock handlers loaded from
    ``examples/`` fixture files; production wiring would instead dispatch
    into the MCP gateway.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[ConnectorCall], Any]] = {}
        # Per-instance tool→connector map that augments the static
        # TOOL_TO_CONNECTOR. Dynamically-registered connectors (e.g. custom
        # REST imports) add their tool names here so resolution doesn't depend
        # on editing the global map.
        self._tool_to_connector: dict[str, str] = {}

    def register(
        self,
        connector: str,
        handler: Callable[[ConnectorCall], Any],
        tools: list[str] | None = None,
    ) -> None:
        """Register a handler for ``connector``.

        ``tools`` optionally declares the tool names this connector serves;
        they are added to the instance tool→connector map so dynamically-added
        connectors resolve without touching the static :data:`TOOL_TO_CONNECTOR`.
        """
        self._handlers[connector] = handler
        for tool in tools or []:
            self._tool_to_connector[tool] = connector

    def resolve(self, tool_name: str) -> str | None:
        """Return the connector serving ``tool_name`` (instance map first)."""
        return self._tool_to_connector.get(tool_name) or TOOL_TO_CONNECTOR.get(tool_name)

    def list_tools(self) -> dict[str, list[str]]:
        """Return ``{connector: [tool, ...]}`` for connectors with a handler.

        Merges the static :data:`TOOL_TO_CONNECTOR` with this instance's
        dynamic registrations (e.g. custom REST imports), then keeps only
        connectors that actually have a registered handler — i.e. what this
        proxy can really dispatch. Used by the ``/v1/connectors`` endpoint so
        operators can confirm their connectors loaded.
        """
        merged: dict[str, set[str]] = {}
        for tool, connector in {**TOOL_TO_CONNECTOR, **self._tool_to_connector}.items():
            merged.setdefault(connector, set()).add(tool)
        return {
            connector: sorted(tools)
            for connector, tools in merged.items()
            if connector in self._handlers
        }

    def has(self, tool_name: str) -> bool:
        connector = self.resolve(tool_name)
        return connector is not None and connector in self._handlers

    async def execute(self, tool_name: str, args: dict[str, Any]) -> tuple[str, Any]:
        """Return ``(connector_name, raw_response)`` or raise KeyError.

        Handlers may be sync (returning a value) or async (returning a
        coroutine). Both forms are awaited transparently so callers in async
        contexts don't need to special-case.
        """
        connector = self.resolve(tool_name)
        if connector is None:
            raise KeyError(f"unknown tool: {tool_name}")
        handler = self._handlers.get(connector)
        if handler is None:
            raise KeyError(f"no handler registered for connector: {connector}")
        call = ConnectorCall(connector=connector, tool=tool_name, args=args)
        result = handler(call)
        if inspect.isawaitable(result):
            result = await result
        return connector, result


# ---------------------------------------------------------------------------
# Mock handlers (used in demo / tests)
# ---------------------------------------------------------------------------


def _load_fixture(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def make_mock_registry(fixtures_dir: Path | str) -> ConnectorRegistry:
    """Build a registry whose ``get_order``/``get_lead`` etc. return fixtures.

    Looks for files named ``<tool>.json`` under ``fixtures_dir``. Missing
    fixtures fall back to a tiny placeholder dict.
    """
    fixtures = Path(fixtures_dir)
    registry = ConnectorRegistry()

    def _handler(call: ConnectorCall) -> Any:
        candidate = fixtures / f"{call.tool}.json"
        if candidate.is_file():
            return _load_fixture(candidate)
        # Generic fallback that still demonstrates a noisy response.
        return {
            "tool": call.tool,
            "args": call.args,
            "result": "mock_default",
            "metadata": {
                "fetched_at": "2026-05-27T10:00:00Z",
                "internal_trace_id": "trace-abc",
                "audit_log": ["...", "...", "..."],
            },
        }

    for connector in set(TOOL_TO_CONNECTOR.values()):
        registry.register(connector, _handler)
    return registry


__all__ = [
    "ConnectorCall",
    "ConnectorRegistry",
    "TOOL_TO_CONNECTOR",
    "make_mock_registry",
]
