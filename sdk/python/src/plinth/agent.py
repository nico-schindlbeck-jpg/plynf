# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Agent decorator and ``AgentContext`` helper.

The decorator turns an ordinary Python function into a small "agent
runner": it materialises the workspace by name, exposes ``ctx`` with
``ctx.tools`` / ``ctx.workspace`` / ``ctx.client``, and otherwise stays
out of the way.

Example::

    @client.agent(workspace="research")
    def run(ctx, topic):
        results = ctx.tools.invoke("web.search", {"query": topic})
        ctx.workspace.kv.set("topic", topic)
        return ctx.workspace.snapshot("done")
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover
    from .client import Plinth
    from .tools import ToolGateway
    from .workspace import Workspace


F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class AgentContext:
    """Runtime context handed to the decorated function as ``ctx``.

    Attributes:
        client: The :class:`Plinth` facade.
        workspace: The auto-resolved :class:`Workspace`.
        tools: A :class:`ToolGateway` whose invocations are pre-tagged
            with this workspace's ID for audit attribution.
        agent_id: Optional caller-supplied agent ID for audit logs.
    """

    client: Plinth
    workspace: Workspace
    tools: ToolGateway
    agent_id: str | None = None


class _ScopedToolGateway:
    """Wrap a ToolGateway so calls auto-include workspace_id / agent_id.

    Implemented as a thin proxy rather than subclassing so we don't have
    to thread state through the gateway's HTTP client.
    """

    def __init__(
        self,
        wrapped: ToolGateway,
        *,
        workspace_id: str,
        agent_id: str | None,
    ) -> None:
        self._wrapped = wrapped
        self._workspace_id = workspace_id
        self._agent_id = agent_id

    # Forward attribute access to the wrapped gateway so unrelated calls
    # (``register``, ``list``, etc.) just work.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    # Override the methods that benefit from auto-tagging.
    def invoke(self, tool_id: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        kwargs.setdefault("workspace_id", self._workspace_id)
        if self._agent_id is not None:
            kwargs.setdefault("agent_id", self._agent_id)
        return self._wrapped.invoke(tool_id, arguments, **kwargs)

    def dry_run(self, tool_id: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        kwargs.setdefault("workspace_id", self._workspace_id)
        if self._agent_id is not None:
            kwargs.setdefault("agent_id", self._agent_id)
        return self._wrapped.dry_run(tool_id, arguments, **kwargs)

    def audit(self, **kwargs: Any) -> Any:
        kwargs.setdefault("workspace_id", self._workspace_id)
        return self._wrapped.audit(**kwargs)


def agent_decorator(
    client: Plinth,
    workspace: str,
    *,
    agent_id: str | None = None,
) -> Callable[[F], F]:
    """Build the decorator returned by :meth:`Plinth.agent`.

    Kept as a free function so it stays unit-testable in isolation.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ws = client.workspace(workspace)
            scoped_tools = _ScopedToolGateway(client.tools, workspace_id=ws.id, agent_id=agent_id)
            ctx = AgentContext(
                client=client,
                workspace=ws,
                tools=scoped_tools,  # type: ignore[arg-type]
                agent_id=agent_id,
            )
            return func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


__all__ = ["AgentContext", "agent_decorator"]
