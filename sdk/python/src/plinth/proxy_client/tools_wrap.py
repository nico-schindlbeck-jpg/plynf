# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Client-side tool wrapping.

Use this when your agent framework (LangChain, CrewAI, AutoGen, custom)
executes tool calls in-process — the Plynf proxy never sees the tool
response, so the response can't be shaped server-side. The wrapper takes
care of that on the client.

Two entry points:

* :func:`wrap_tool` — wrap a single callable. The wrapped callable runs the
  original, then POSTs the raw response to Plynf's ``/v1/shape`` endpoint
  for shaping under your policy, and returns the shaped response.

* :func:`wrap_tools` — convenience for lists.

Framework adapters that need a particular tool surface (LangChain's
``BaseTool``, CrewAI's ``@tool`` decorator) can wrap their tools with this
generic helper without inheriting from any framework class.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ShapeContext:
    plynf_url: str
    api_key: str
    tenant_id: str | None = None
    timeout_s: float = 30.0


class ShapeError(RuntimeError):
    pass


def _post_shape(ctx: ShapeContext, tool: str, raw: Any) -> Any:
    body = {"tool": tool, "raw_response": raw}
    if ctx.tenant_id:
        body["tenant_id"] = ctx.tenant_id
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ctx.api_key}",
    }
    url = ctx.plynf_url.rstrip("/") + "/v1/shape"
    with httpx.Client(timeout=ctx.timeout_s) as client:
        resp = client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        raise ShapeError(f"plynf shape returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data.get("shaped", raw)


def wrap_tool(
    func: Callable[..., Any],
    *,
    plynf_url: str,
    api_key: str,
    tool_name: str | None = None,
    tenant_id: str | None = None,
) -> Callable[..., Any]:
    """Return a callable that runs ``func`` and then asks Plynf to shape the result.

    ``tool_name`` defaults to ``func.__name__`` so the Plynf policy engine
    can match it against the connector registry. If the name doesn't match
    a known policy, Plynf returns the raw response unchanged.
    """

    ctx = ShapeContext(plynf_url=plynf_url, api_key=api_key, tenant_id=tenant_id)
    name = tool_name or getattr(func, "__name__", "anonymous_tool")

    @functools.wraps(func)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        raw = func(*args, **kwargs)
        try:
            return _post_shape(ctx, name, raw)
        except ShapeError:
            # Fail-open: if Plynf is unreachable, agent still works (with the
            # raw response). The Plynf-Cloud SLA covers shape availability.
            return raw

    # Tag for introspection so frameworks can show "wrapped by plynf" badges.
    _wrapped.__plynf_wrapped__ = True  # type: ignore[attr-defined]
    _wrapped.__plynf_tool_name__ = name  # type: ignore[attr-defined]
    return _wrapped


def wrap_tools(
    funcs: list[Callable[..., Any]],
    *,
    plynf_url: str,
    api_key: str,
    tenant_id: str | None = None,
) -> list[Callable[..., Any]]:
    """Wrap a list of tool callables. Names taken from ``__name__``."""
    return [
        wrap_tool(f, plynf_url=plynf_url, api_key=api_key, tenant_id=tenant_id)
        for f in funcs
    ]


__all__ = ["ShapeContext", "ShapeError", "wrap_tool", "wrap_tools"]
