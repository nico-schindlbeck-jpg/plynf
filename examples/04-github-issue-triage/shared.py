# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared helpers for the GitHub issue-triage example.

The agent itself lives in ``triage_agent.py``. This module owns:

* The mock LLM (rule-based, deterministic). Real LLMs would replace this with
  an actual API call; the example keeps it offline so the demo runs anywhere.
* The fixture issues used in ``--mode=simulation``.
* Service endpoints + small wrappers for talking to the gateway.

Two design principles:

1. ``--mode=simulation`` MUST run end-to-end without any external network
   calls. That means the fixtures here cover both the issue list and the
   classification.
2. ``--mode=live`` only changes the *transport* (gateway → github-mcp →
   api.github.com). The classification logic is identical.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx


GATEWAY_URL = os.environ.get("PLINTH_GATEWAY_URL", "http://localhost:7422")
GITHUB_MCP_URL = os.environ.get("PLINTH_GITHUB_MCP_URL", "http://localhost:7426")
API_KEY = os.environ.get("PLINTH_API_KEY", "local-dev")


# ---------------------------------------------------------------------------
# Classification labels
# ---------------------------------------------------------------------------


Category = Literal["bug", "feature", "question", "spam"]


@dataclass
class ClassifiedIssue:
    """Result of running the LLM over one issue."""

    number: int
    title: str
    url: str | None
    user: str | None
    category: Category
    rationale: str
    confidence: float
    labels_existing: list[str] = field(default_factory=list)
    body_preview: str = ""


@dataclass
class TriageReport:
    """Aggregated report written to the workspace at the end of the run."""

    repo: str
    mode: str
    total_issues: int = 0
    classifications: list[ClassifiedIssue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    cached_lookups: int = 0
    live_lookups: int = 0


# ---------------------------------------------------------------------------
# Mock LLM — deterministic rule-based classifier
# ---------------------------------------------------------------------------


_BUG_KEYWORDS = (
    "bug",
    "crash",
    "error",
    "broken",
    "regression",
    "stack trace",
    "fail",
    "exception",
    "incorrect",
)
_FEATURE_KEYWORDS = (
    "feature",
    "request",
    "would be nice",
    "add support",
    "enhancement",
    "proposal",
    "new option",
)
_QUESTION_KEYWORDS = (
    "?",
    "how do i",
    "is there",
    "how to",
    "could you",
    "documentation",
    "what is",
)
_SPAM_KEYWORDS = (
    "click here",
    "buy now",
    "earn money",
    "🎉🎉🎉",
    "100% free",
    "promotional",
)


def classify_issue(title: str, body: str | None) -> ClassifiedIssue:
    """Stub classifier. Real demos would call an LLM; we keep this offline.

    The function returns a :class:`ClassifiedIssue` *without* the
    issue-identifier fields filled in; callers merge in ``number`` / ``url``.
    """
    text = f"{title}\n\n{body or ''}".lower()

    # Spam first — most decisive when it triggers.
    if any(kw in text for kw in _SPAM_KEYWORDS):
        return ClassifiedIssue(
            number=0, title=title, url=None, user=None,
            category="spam",
            rationale="matched spam keywords",
            confidence=0.95,
        )
    if any(kw in text for kw in _BUG_KEYWORDS):
        return ClassifiedIssue(
            number=0, title=title, url=None, user=None,
            category="bug",
            rationale="title/body indicates a defect",
            confidence=0.8,
        )
    if any(kw in text for kw in _FEATURE_KEYWORDS):
        return ClassifiedIssue(
            number=0, title=title, url=None, user=None,
            category="feature",
            rationale="reads like a feature request",
            confidence=0.75,
        )
    if any(kw in text for kw in _QUESTION_KEYWORDS):
        return ClassifiedIssue(
            number=0, title=title, url=None, user=None,
            category="question",
            rationale="phrased as a question",
            confidence=0.65,
        )
    return ClassifiedIssue(
        number=0, title=title, url=None, user=None,
        category="question",
        rationale="no strong signal — defaulting to question for human review",
        confidence=0.4,
    )


# ---------------------------------------------------------------------------
# Simulation fixtures
# ---------------------------------------------------------------------------


SIMULATION_ISSUES: list[dict[str, Any]] = [
    {
        "number": 101,
        "title": "Crash on startup when config is missing",
        "body": "The CLI fails with a stack trace if I run it without a config file. "
                "Expected behaviour is to print a friendly error.",
        "url": "https://github.com/demo/repo/issues/101",
        "user": "alice",
        "labels": [],
    },
    {
        "number": 102,
        "title": "Add support for YAML config",
        "body": "It would be nice to add YAML config support next to TOML.",
        "url": "https://github.com/demo/repo/issues/102",
        "user": "bob",
        "labels": [],
    },
    {
        "number": 103,
        "title": "How do I configure the cache TTL?",
        "body": "I can't find documentation on how to set a custom cache TTL.",
        "url": "https://github.com/demo/repo/issues/103",
        "user": "carol",
        "labels": [],
    },
    {
        "number": 104,
        "title": "Buy our SEO services 🎉🎉🎉 click here",
        "body": "100% free promotional offer just for you!",
        "url": "https://github.com/demo/repo/issues/104",
        "user": "spammer42",
        "labels": [],
    },
    {
        "number": 105,
        "title": "Regression: list endpoint returns 500 on empty repos",
        "body": "After upgrading to v0.3 the list endpoint throws an exception when "
                "the repo is empty. Was working in v0.2.",
        "url": "https://github.com/demo/repo/issues/105",
        "user": "dave",
        "labels": ["needs-triage"],
    },
    {
        "number": 106,
        "title": "Feature request: add CSV export",
        "body": "Could the workspace files API expose a CSV export option?",
        "url": "https://github.com/demo/repo/issues/106",
        "user": "erin",
        "labels": [],
    },
    {
        "number": 107,
        "title": "Is there a way to filter audit events by tenant?",
        "body": "I'd like to see audit events for one tenant at a time.",
        "url": "https://github.com/demo/repo/issues/107",
        "user": "frank",
        "labels": [],
    },
    {
        "number": 108,
        "title": "Incorrect cost estimate for cached calls",
        "body": "The dashboard shows non-zero cost for tool calls served from the "
                "cache. Should be zero.",
        "url": "https://github.com/demo/repo/issues/108",
        "user": "grace",
        "labels": ["bug"],
    },
    {
        "number": 109,
        "title": "Proposal: snapshot retention policy",
        "body": "Add an option to auto-prune old snapshots after N days.",
        "url": "https://github.com/demo/repo/issues/109",
        "user": "harry",
        "labels": [],
    },
    {
        "number": 110,
        "title": "Question on workflow resume semantics",
        "body": "Does workflow resume restore the workspace KV at the snapshot point?",
        "url": "https://github.com/demo/repo/issues/110",
        "user": "ivy",
        "labels": [],
    },
]


def simulation_issues(limit: int) -> list[dict[str, Any]]:
    """Return the first ``limit`` simulation issues (capped at the fixture size)."""
    if limit <= 0:
        return []
    return list(SIMULATION_ISSUES[:limit])


# ---------------------------------------------------------------------------
# Live mode — gateway client
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


@dataclass
class GatewayClient:
    """Tiny synchronous client over the Plinth gateway's invoke endpoint.

    We avoid the SDK here so the demo has zero non-stdlib import surprises.
    """

    base_url: str = GATEWAY_URL
    api_key: str = API_KEY
    workspace_id: str = "github-triage"
    agent_id: str = "github-triage-agent"

    def invoke(self, tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = {
            "tool_id": tool_id,
            "arguments": arguments,
            "workspace_id": self.workspace_id,
            "agent_id": self.agent_id,
        }
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self.base_url.rstrip('/')}/v1/invoke",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"error": {"code": "INVALID_RESPONSE", "message": resp.text}}
            raise RuntimeError(f"gateway invoke failed: {err}")
        return resp.json()


def gateway_available() -> bool:
    """Return True if the gateway and the github-mcp are reachable."""
    try:
        with httpx.Client(timeout=2.0) as c:
            c.get(f"{GATEWAY_URL.rstrip('/')}/healthz").raise_for_status()
            c.get(f"{GITHUB_MCP_URL.rstrip('/')}/healthz").raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(report: TriageReport) -> str:
    """Render the triage report as Markdown for the workspace file."""
    lines: list[str] = []
    lines.append(f"# Triage report — {report.repo}")
    lines.append("")
    lines.append(f"_Mode_: `{report.mode}`")
    lines.append(f"_Total issues triaged_: {report.total_issues}")
    lines.append("")
    lines.append("## Counts")
    for cat in ("bug", "feature", "question", "spam"):
        lines.append(f"- **{cat}**: {report.counts.get(cat, 0)}")
    lines.append("")
    lines.append("## Per-issue classifications")
    for c in report.classifications:
        lines.append(f"### #{c.number} — {c.title}")
        lines.append(f"- _category_: **{c.category}**")
        lines.append(f"- _confidence_: {c.confidence:.2f}")
        lines.append(f"- _rationale_: {c.rationale}")
        if c.user:
            lines.append(f"- _author_: {c.user}")
        if c.url:
            lines.append(f"- _url_: <{c.url}>")
        if c.labels_existing:
            lines.append(f"- _existing labels_: {', '.join(c.labels_existing)}")
        if c.body_preview:
            lines.append("")
            lines.append("> " + c.body_preview.strip().replace("\n", "\n> "))
        lines.append("")
    return "\n".join(lines)


def slug(value: str) -> str:
    """Public re-export of :func:`_slug` for the agent module."""
    return _slug(value)
