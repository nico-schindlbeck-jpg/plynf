# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The Plinth-enabled research agent.

The architectural difference vs. the baseline:

* Sources are stored in a workspace by key, **never inlined into the
  LLM prompt**. The agent reads a single source's content, extracts
  facts, writes the facts back, then drops the content from working
  memory before moving to the next source.
* The synthesis step references only the structured facts (small),
  not the raw source content. The synthesis prompt fits in roughly
  one-fifth of the baseline context size.
* Tool calls go through the gateway's caching layer, so duplicate
  fetches are free. (When the gateway is not running we simulate the
  caching in-process so the demo still reflects the value prop.)
* Snapshots are taken at logical checkpoints; in production this gives
  resumability and audit. The demo records them but does not exercise
  resume in v0.1.

The token-accounting code is identical to ``baseline.py`` — both go
through ``shared.llm_call``. The only thing that differs is *what gets
sent in the prompt*.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from shared import (
    ResearchReport,
    ToolCallRecord,
    get_tool_backend,
    llm_call,
    load_topics_config,
    services_available,
    slugify,
)


# ---------------------------------------------------------------------------
# Workspace + Gateway facade
# ---------------------------------------------------------------------------
#
# We try the real SDK; if it doesn't expose the high-level facade yet
# (the workspace and tools sub-clients aren't wired in v0.1), we fall
# back to an in-process simulation that has the same observable
# semantics: KV writes are stored, reads return them; gateway calls are
# cached by ``(tool_id, arguments)``.


class _InProcessKV:
    """Simulated workspace.kv — values stored in memory."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str) -> Any:
        return self._data.get(key)


class _InProcessFiles:
    """Simulated workspace.files — bytes stored in memory."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def write(self, path: str, content: str) -> None:
        self._data[path] = content

    def read(self, path: str, *, as_text: bool = True) -> str:
        return self._data.get(path, "")


class _InProcessWorkspace:
    """Simulated workspace exposing kv/files/snapshot semantics."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.kv = _InProcessKV()
        self.files = _InProcessFiles()
        self._snapshots: list[str] = []

    def snapshot(self, name: str, message: str | None = None) -> str:
        self._snapshots.append(name)
        return name


class _InProcessTools:
    """Simulated gateway tools.invoke with a TTL-free cache.

    The point of this class is to mirror gateway caching semantics so
    the comparison reflects the value of the gateway even when no
    services are running. The cache key is the same hash the real
    gateway uses: ``sha256(tool_id || canonical_json(arguments))``.
    """

    def __init__(self, backend: Any, record: ResearchReport) -> None:
        self._backend = backend
        self._record = record
        self._cache: dict[tuple[str, str], Any] = {}

    def invoke(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        cache: bool = True,
    ) -> dict[str, Any]:
        import json

        cache_key = (tool_id, json.dumps(arguments, sort_keys=True))
        cached = cache and cache_key in self._cache
        if cached:
            self._record.tool_calls.append(
                ToolCallRecord(
                    tool=tool_id, arguments=arguments, cached=True, duration_ms=0
                )
            )
            return {"result": self._cache[cache_key], "cached": True}

        start = time.perf_counter()
        if tool_id == "web.search":
            res = self._backend.search(
                arguments.get("query", ""), k=int(arguments.get("k", 5))
            )
        elif tool_id == "web.fetch":
            res = self._backend.fetch(arguments["url"])
        else:
            raise ValueError(f"Unknown tool {tool_id!r}")
        duration_ms = int((time.perf_counter() - start) * 1000)
        if cache:
            self._cache[cache_key] = res
        self._record.tool_calls.append(
            ToolCallRecord(
                tool=tool_id, arguments=arguments, cached=False, duration_ms=duration_ms
            )
        )
        return {"result": res, "cached": False}


_TOOL_REGISTRATIONS: list[dict[str, Any]] = [
    {
        "tool_id": "web.search",
        "name": "Web search",
        "description": "Search the web; returns a list of source URLs and snippets.",
        "transport": "http",
        "endpoint": "http://localhost:7423/invoke/web.search",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
            "required": ["query"],
        },
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 3600,
        "auth_method": "none",
        "auth_config": {},
    },
    {
        "tool_id": "web.fetch",
        "name": "Fetch URL",
        "description": "Fetch a URL and return its text content.",
        "transport": "http",
        "endpoint": "http://localhost:7423/invoke/web.fetch",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 3600,
        "auth_method": "none",
        "auth_config": {},
    },
]


def _ensure_tools_registered(client: Any) -> None:
    """Register the demo's tools with the gateway. Idempotent."""
    try:
        existing = {t.tool_id for t in client.tools.list()}
    except Exception:  # noqa: BLE001
        existing = set()
    for reg in _TOOL_REGISTRATIONS:
        if reg["tool_id"] in existing:
            continue
        try:
            client.tools.register(reg)
        except Exception:  # noqa: BLE001 — best-effort; if reg fails, fallback path will catch
            pass


def _unwrap_invoke(resp: Any) -> dict[str, Any]:
    """Extract the inner tool result from an SDK or simulated response.

    The SDK returns an :class:`~plinth.models.InvokeResponse` (Pydantic),
    where ``response.result`` holds whatever the backend returned. The
    in-process simulation returns ``{"result": ..., "cached": ...}``.
    Mock-MCP itself wraps its response in ``{"result": ...}``, so the
    SDK path may have a doubly-wrapped payload — we unwrap that too.
    """
    # Pydantic model from the SDK
    if hasattr(resp, "result"):
        inner = resp.result
    elif isinstance(resp, dict) and "result" in resp:
        inner = resp["result"]
    else:
        inner = resp
    # Mock-MCP's outer "result" envelope (when fetched via gateway proxy)
    if isinstance(inner, dict) and set(inner.keys()) == {"result"}:
        inner = inner["result"]
    elif isinstance(inner, dict) and "result" in inner and len(inner) == 1:
        inner = inner["result"]
    return inner


def _get_plinth_facade(
    workspace_name: str, backend: Any, record: ResearchReport
) -> tuple[Any, Any, str]:
    """Try the real SDK; fall back to in-process simulation.

    Returns ``(workspace, tools, kind)`` where ``kind`` is either
    ``"sdk"`` or ``"simulated"``.
    """
    try:  # noqa: SIM105 — explicit so we can mention the import error
        from plinth import Plinth  # type: ignore[attr-defined]

        services = services_available()
        # All three services must be available for the real SDK path:
        # workspace (state), gateway (tool routing), mock_mcp (tool backend).
        # If mock_mcp is missing, the gateway has nothing to proxy to,
        # so we fall back to in-process simulation.
        if services["workspace"] and services["gateway"] and services["mock_mcp"]:
            client = Plinth(
                workspace_url="http://localhost:7421",
                gateway_url="http://localhost:7422",
                api_key="local-dev",
            )
            # Ensure tools are registered with the gateway. Idempotent — if
            # they exist already, register raises but we swallow it.
            _ensure_tools_registered(client)
            ws = client.workspace(workspace_name)
            return ws, client.tools, "sdk"
    except (ImportError, AttributeError):
        pass
    # Simulated path covers v0.1 where the SDK class isn't built out.
    return (
        _InProcessWorkspace(workspace_name),
        _InProcessTools(backend, record),
        "simulated",
    )


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run_with_plinth(topic: str, mode: str = "simulation") -> ResearchReport:
    """Run the Plinth-enabled agent end-to-end on ``topic``.

    The structural difference from the baseline is in this function's
    body, not in any call signatures: the LLM history is reset to a
    minimal slice for each reasoning step, and source content is read
    from the workspace by key only when it's actually needed.
    """
    record = ResearchReport(topic=topic, report_text="", sources=[])
    backend, _backend_kind = get_tool_backend()
    workspace_name = f"research-{slugify(topic)}"
    ws, tools, _facade_kind = _get_plinth_facade(workspace_name, backend, record)

    start = time.perf_counter()

    # ------------------------------------------------------------------
    # Step 1 — search via the gateway. Cache-key is the query, so a
    # rerun on the same topic is free in this slot.
    # ------------------------------------------------------------------
    search_history: list[tuple[str, str]] = [
        (
            "user",
            f"You are a research agent on the Plinth substrate. "
            f"Search for sources on '{topic}' using the web.search tool.",
        ),
    ]
    llm_call(
        search_history,
        step="decide-search",
        purpose="short",
        topic=topic,
        mode=mode,
        record=record,
    )
    search = tools.invoke("web.search", {"query": topic, "k": 5})
    sources = _unwrap_invoke(search)["results"]
    record.sources = sources

    ws.kv.set("topic", topic)
    ws.kv.set("sources/index", [s["url"] for s in sources])
    for src in sources:
        ws.kv.set(f"sources/meta/{src['url']}", {"title": src["title"], "fetched": False})

    # ------------------------------------------------------------------
    # Step 2 — fetch each source via the gateway. Cache hits are free.
    # The fetched content goes to workspace storage, NOT into LLM
    # history. This is the central architectural shift.
    # ------------------------------------------------------------------
    for src in sources:
        fetched = tools.invoke("web.fetch", {"url": src["url"]})
        content = _unwrap_invoke(fetched)["content"]
        ws.files.write(f"sources/{slugify(src['url'])}.txt", content)
        meta = ws.kv.get(f"sources/meta/{src['url']}") or {}
        meta["fetched"] = True
        ws.kv.set(f"sources/meta/{src['url']}", meta)

    ws.snapshot("sources-collected", message="all 5 sources fetched and stored")

    # ------------------------------------------------------------------
    # Step 3 — extract facts source-by-source. Each LLM call sees ONLY
    # the source it's working on, plus a small system prompt. The token
    # cost is per-source linear, not super-linear like the baseline.
    # ------------------------------------------------------------------
    facts_by_url: dict[str, str] = {}
    for src in sources:
        content = ws.files.read(f"sources/{slugify(src['url'])}.txt", as_text=True)
        per_source_history: list[tuple[str, str]] = [
            (
                "user",
                f"Extract 3-5 key facts from the following source.\n\n"
                f"Source title: {src['title']}\n"
                f"Source URL: {src['url']}\n\n"
                f"---\n{content}\n---",
            ),
        ]
        response = llm_call(
            per_source_history,
            step=f"extract:{src['url']}",
            purpose="extraction",
            topic=topic,
            mode=mode,
            record=record,
        )
        facts_by_url[src["url"]] = response
        ws.kv.set(f"facts/{src['url']}", response)

    ws.snapshot("facts-extracted", message="per-source facts written to KV")

    # ------------------------------------------------------------------
    # Step 4 — synthesise. Prompt has the structured facts only, not
    # the raw sources. This is what makes the synthesis step cheap.
    # ------------------------------------------------------------------
    facts_summary = "\n\n".join(
        f"### Facts from {url}\n{facts}" for url, facts in facts_by_url.items()
    )
    synth_history: list[tuple[str, str]] = [
        (
            "user",
            f"Synthesise a 500-1000 word markdown report on '{topic}' "
            f"using the following extracted facts. Cite each source by URL "
            f"in the report.\n\n{facts_summary}",
        ),
    ]
    report_text = llm_call(
        synth_history,
        step="synthesise",
        purpose="synthesis",
        topic=topic,
        mode=mode,
        record=record,
    )
    ws.files.write("report.md", report_text)
    ws.snapshot("report-final", message="final report written")

    record.report_text = report_text
    record.wall_clock_seconds = time.perf_counter() - start
    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: ResearchReport) -> None:
    print()
    print(f"Plinth agent — topic: {report.topic!r}")
    print(f"  LLM calls          : {report.llm_call_count}")
    print(f"  Input tokens       : {report.total_input_tokens:,}")
    print(f"  Output tokens      : {report.total_output_tokens:,}")
    print(f"  Total tokens       : {report.total_tokens:,}")
    print(f"  Estimated cost USD : {report.total_cost_usd:.4f}")
    print(f"  Tool calls         : {report.tool_call_count} ")
    print(f"  Cached tool calls  : {report.cached_tool_calls}")
    print(f"  Wall clock         : {report.wall_clock_seconds:.2f}s")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plinth-enabled research agent.")
    parser.add_argument("--topic", default=None, help="Research topic.")
    parser.add_argument(
        "--mode",
        default="simulation",
        choices=["simulation", "mock-llm", "live"],
        help="LLM mode (simulation/mock-llm are equivalent).",
    )
    args = parser.parse_args(argv)

    topic = args.topic or load_topics_config().get("default_topic", "renewable energy")
    mode = "simulation" if args.mode == "mock-llm" else args.mode

    services = services_available()
    if all(services.values()):
        print("[plinth] All Plinth services reachable; using gateway/workspace.")
    else:
        missing = [k for k, v in services.items() if not v]
        print(
            f"[plinth] Services not reachable: {missing}. "
            "Falling back to in-process simulation of workspace + gateway."
        )

    report = run_with_plinth(topic, mode=mode)
    _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
