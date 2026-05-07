# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared helpers for the durable-workflow example.

* Mock LLM and search/fetch sources so the demo runs offline.
* A small ``services_available`` probe to bail out cleanly when the
  user hasn't started Plinth.
* Slugify helper used by both the handlers and the start script.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Service detection
# ---------------------------------------------------------------------------


def services_available() -> dict[str, bool]:
    """Return a ``{service: reachable}`` map.

    Used by ``start_workflow.py`` to gate the demo on the workspace
    actually being up — the example REQUIRES it (unlike example 03 which
    has an in-process fallback).
    """

    services: dict[str, str] = {
        "workspace": os.environ.get("PLINTH_WORKSPACE_URL", "http://localhost:7421"),
        "gateway": os.environ.get("PLINTH_GATEWAY_URL", "http://localhost:7422"),
    }
    out: dict[str, bool] = {}
    for name, url in services.items():
        try:
            r = httpx.get(f"{url}/healthz", timeout=1.0)
            out[name] = r.status_code == 200
        except (httpx.HTTPError, OSError):
            out[name] = False
    return out


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Squash a URL or phrase into a filesystem-safe slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text)
    return s.strip("-").lower()[:80]


def mock_search(topic: str, k: int = 5) -> list[dict[str, str]]:
    """Return a stable list of ``k`` mock sources keyed off ``topic``."""

    base = topic.replace(" ", "-").lower()
    return [
        {
            "url": f"mock://{base}/{i}",
            "title": f"{topic.title()} Insight #{i}",
            "snippet": f"A short excerpt about {topic} (mock source #{i}).",
        }
        for i in range(1, k + 1)
    ]


def mock_fetch(url: str) -> str:
    """Return canned content for a mock URL."""
    return (
        f"# Source content for {url}\n\n"
        "This is a mock fetched document used by the durable-workflow demo. "
        "Each source paragraph is intentionally distinct so the synthesise "
        "step has something to weave together.\n\n"
        f"Body: lorem ipsum about {url}, with three or four sentences. "
        "Ending with a clear takeaway.\n"
    )


def mock_extract(content: str, *, topic: str) -> list[str]:
    """Pull a few synthetic 'facts' out of mock content."""
    return [
        f"Fact about {topic} from {len(content)} chars of source",
        f"Key driver of {topic} mentioned in the source",
        f"Risk factor for {topic} highlighted in the source",
    ]


def mock_synthesise(topic: str, facts_by_url: dict[str, list[str]]) -> str:
    """Compose a mock report from gathered facts."""
    lines = [f"# Report: {topic}", ""]
    lines.append(f"This synthetic report on **{topic}** weaves together facts ")
    lines.append(f"from {len(facts_by_url)} mock sources.\n")
    for url, facts in facts_by_url.items():
        lines.append(f"## Source: {url}")
        for f in facts:
            lines.append(f"- {f}")
        lines.append("")
    lines.append("## Conclusion\n")
    lines.append(
        f"The combined evidence suggests {topic} is multi-faceted; further "
        "investigation is recommended."
    )
    return "\n".join(lines)


__all__ = [
    "mock_extract",
    "mock_fetch",
    "mock_search",
    "mock_synthesise",
    "services_available",
    "slugify",
]


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def env(name: str, default: str) -> str:
    """Mini wrapper around os.environ.get with a default."""
    return os.environ.get(name, default)


def make_client_kwargs() -> dict[str, Any]:
    """Build the kwargs both the worker and the start script use."""
    return {
        "workspace_url": env("PLINTH_WORKSPACE_URL", "http://localhost:7421"),
        "gateway_url": env("PLINTH_GATEWAY_URL", "http://localhost:7422"),
        "api_key": env("PLINTH_API_KEY", "local-dev"),
    }
