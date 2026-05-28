# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""LangChain-native helpers — optional, only loaded if langchain is installed.

Two entry points:

  * :func:`make_plynf_tool` — build a LangChain :class:`StructuredTool` whose
    function transparently calls Plynf for shaping. Drop into any LangChain
    agent that takes a ``tools=[...]`` list.

  * :func:`wrap_langchain_tools` — given an existing list of LangChain
    ``BaseTool`` instances, return a parallel list whose ``_run``/``_arun``
    methods route through Plynf. Lets you keep your existing tool factory
    code and add shaping at the boundary.

We import langchain lazily so installing ``plinth`` doesn't pull
~hundreds-of-MB transitive LangChain deps for users who don't need them.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .tools_wrap import ShapeContext, _post_shape


def make_plynf_tool(
    fn: Callable[..., Any],
    *,
    plynf_url: str,
    api_key: str,
    name: str | None = None,
    description: str | None = None,
    tenant_id: str | None = None,
    args_schema: Any = None,
):
    """Return a LangChain :class:`StructuredTool` that shapes ``fn`` output.

    Requires ``langchain`` (``pip install langchain``). The import is
    inside this function so the rest of the SDK works without LangChain.
    """
    try:
        from langchain_core.tools import StructuredTool  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "langchain-core is required for make_plynf_tool. "
            "Install with: pip install langchain-core"
        ) from e

    ctx = ShapeContext(plynf_url=plynf_url, api_key=api_key, tenant_id=tenant_id)
    tool_name = name or getattr(fn, "__name__", "anonymous_tool")
    tool_desc = description or (fn.__doc__ or f"{tool_name} — Plynf-shaped")

    @functools.wraps(fn)
    def _shaped(*args: Any, **kwargs: Any) -> Any:
        raw = fn(*args, **kwargs)
        try:
            return _post_shape(ctx, tool_name, raw)
        except Exception:  # noqa: BLE001 — fail open, return raw
            return raw

    return StructuredTool.from_function(
        func=_shaped,
        name=tool_name,
        description=tool_desc,
        args_schema=args_schema,
    )


def wrap_langchain_tools(
    tools: list[Any],
    *,
    plynf_url: str,
    api_key: str,
    tenant_id: str | None = None,
) -> list[Any]:
    """Wrap each tool's ``_run`` so its returned value goes through Plynf.

    Mutates ``StructuredTool`` / ``BaseTool`` subclasses in-place where
    possible; falls back to building a new :class:`StructuredTool` if the
    tool's ``_run`` can't be replaced (e.g. read-only Pydantic v1 models).
    """
    ctx = ShapeContext(plynf_url=plynf_url, api_key=api_key, tenant_id=tenant_id)
    out: list[Any] = []
    for tool in tools:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", "unknown_tool")
        original_run = getattr(tool, "_run", None) or getattr(tool, "run", None)
        if original_run is None:
            out.append(tool)
            continue

        def _make_wrapper(orig, tname):
            def _shaped(*args, **kwargs):
                raw = orig(*args, **kwargs)
                try:
                    return _post_shape(ctx, tname, raw)
                except Exception:  # noqa: BLE001
                    return raw

            return _shaped

        wrapped = _make_wrapper(original_run, name)
        try:
            tool._run = wrapped  # type: ignore[attr-defined]
            tool.__plynf_wrapped__ = True
            out.append(tool)
        except Exception:  # noqa: BLE001 — pydantic immutability etc.
            out.append(make_plynf_tool(
                wrapped,
                plynf_url=plynf_url,
                api_key=api_key,
                name=name,
                description=getattr(tool, "description", "") or name,
                tenant_id=tenant_id,
            ))
    return out


__all__ = ["make_plynf_tool", "wrap_langchain_tools"]
