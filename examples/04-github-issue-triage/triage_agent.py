# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Plinth example: GitHub issue triage.

What this agent does, end-to-end:

1. List open issues for a repo (via the gateway → github-mcp → GitHub REST,
   or via fixtures in simulation mode).
2. For each issue: fetch the full body + classify it (bug | feature | question
   | spam) using a deterministic mock LLM in this example, then collect the
   classifications.
3. Group findings into buckets and write a Markdown triage report to a
   workspace location (in simulation mode this is a local file under
   ``./reports/``; in live mode the same file goes to a Plinth workspace
   blob).
4. Optionally post a summary comment on each issue (off by default; gated by
   ``--post-comments``).

CLI:

```
python triage_agent.py --repo demo/repo --limit 10 --mode simulation
python triage_agent.py --repo myorg/myrepo --mode live  # actually calls GitHub
```

In live mode the agent assumes:

* The Plinth gateway is running at ``$PLINTH_GATEWAY_URL`` (default
  ``http://localhost:7422``).
* The github-mcp is running at ``$PLINTH_GITHUB_MCP_URL`` (default
  ``http://localhost:7426``).
* You have already completed the OAuth flow once and obtained a connection
  ``conn_<ulid>``. Pass it via ``$PLINTH_GITHUB_CONNECTION_ID`` or the
  ``--connection-id`` flag.
* The github-mcp tools have been registered with the gateway and reference
  the connection via ``auth_config.connection_id``.

The README walks you through OAuth setup the first time.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from shared import (
    ClassifiedIssue,
    GatewayClient,
    TriageReport,
    classify_issue,
    gateway_available,
    render_report,
    simulation_issues,
    slug,
)


REPORTS_DIR = Path(__file__).parent / "reports"


# ---------------------------------------------------------------------------
# Simulation backend
# ---------------------------------------------------------------------------


class SimulationBackend:
    """Returns canned issue fixtures with no network I/O."""

    def __init__(self, repo: str, limit: int) -> None:
        self.repo = repo
        self.limit = limit

    def list_issues(self) -> list[dict[str, Any]]:
        return simulation_issues(self.limit)

    def get_issue(self, number: int) -> dict[str, Any]:
        for issue in simulation_issues(len(simulation_issues(100))):
            if issue["number"] == number:
                return issue
        raise KeyError(number)

    def post_comment(self, number: int, body: str) -> dict[str, Any]:
        return {"comment": {"id": -1, "body": body, "user": {"login": "triage-bot"}}}


# ---------------------------------------------------------------------------
# Live backend (via the gateway)
# ---------------------------------------------------------------------------


class LiveBackend:
    """Hits the Plinth gateway, which proxies to github-mcp + GitHub."""

    def __init__(self, repo: str, limit: int, *, gateway: GatewayClient) -> None:
        self.repo = repo
        self.limit = limit
        self.gateway = gateway

    def list_issues(self) -> list[dict[str, Any]]:
        result = self.gateway.invoke(
            "github.list_issues", {"repo": self.repo, "per_page": self.limit}
        )
        # Gateway invoke wraps result; github-mcp returns {"issues": [...]}
        # inside a {"result": ...} envelope. Unwrap both layers.
        inner = result.get("result", {})
        if isinstance(inner, dict) and "result" in inner and isinstance(inner["result"], dict):
            inner = inner["result"]
        issues_raw = inner.get("issues", [])
        normalised: list[dict[str, Any]] = []
        for raw in issues_raw[: self.limit]:
            user = (raw.get("user") or {}).get("login")
            normalised.append(
                {
                    "number": raw.get("number"),
                    "title": raw.get("title"),
                    "body": raw.get("body"),
                    "url": raw.get("url"),
                    "user": user,
                    "labels": raw.get("labels") or [],
                }
            )
        return normalised

    def get_issue(self, number: int) -> dict[str, Any]:
        result = self.gateway.invoke(
            "github.get_issue", {"repo": self.repo, "number": number}
        )
        inner = result.get("result", {})
        if isinstance(inner, dict) and "result" in inner and isinstance(inner["result"], dict):
            inner = inner["result"]
        issue = inner.get("issue") or {}
        user = (issue.get("user") or {}).get("login")
        return {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "body": issue.get("body"),
            "url": issue.get("url"),
            "user": user,
            "labels": issue.get("labels") or [],
        }

    def post_comment(self, number: int, body: str) -> dict[str, Any]:
        return self.gateway.invoke(
            "github.comment_on_issue",
            {"repo": self.repo, "number": number, "body": body},
        )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def _summary_comment(c: ClassifiedIssue) -> str:
    return (
        f"Plinth triage bot says: this looks like a **{c.category}** "
        f"({c.confidence:.0%} confidence). Rationale: {c.rationale}.\n\n"
        "(This was generated by an automated agent; a human will follow up.)"
    )


def run(
    *,
    repo: str,
    mode: str,
    limit: int,
    post_comments: bool,
    backend: Any,
) -> TriageReport:
    """Run the triage flow against ``backend`` (simulation or live)."""
    report = TriageReport(repo=repo, mode=mode)
    issues = backend.list_issues()
    report.total_issues = len(issues)

    for issue in issues:
        existing_labels = [
            (label["name"] if isinstance(label, dict) else str(label))
            for label in issue.get("labels", [])
        ]
        title = issue.get("title", "<no title>")
        body = issue.get("body") or ""
        cls = classify_issue(title, body)
        cls.number = int(issue.get("number") or 0)
        cls.title = title
        cls.url = issue.get("url")
        cls.user = issue.get("user")
        cls.labels_existing = existing_labels
        cls.body_preview = (body[:240] + "…") if len(body) > 240 else body
        report.classifications.append(cls)

        if post_comments:
            try:
                backend.post_comment(cls.number, _summary_comment(cls))
            except Exception as exc:  # noqa: BLE001
                print(f"[triage] failed to post comment on #{cls.number}: {exc}")

    counter: Counter[str] = Counter(c.category for c in report.classifications)
    report.counts = dict(counter)
    return report


def write_report(report: TriageReport) -> Path:
    """Write the rendered Markdown report under ``./reports/`` and return its path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"triage-{slug(report.repo)}-{report.mode}.md"
    path = REPORTS_DIR / filename
    path.write_text(render_report(report), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: TriageReport, report_path: Path) -> None:
    print()
    print(f"Triage agent — repo: {report.repo!r} (mode: {report.mode})")
    print(f"  Issues triaged     : {report.total_issues}")
    for cat in ("bug", "feature", "question", "spam"):
        print(f"  {cat:<18}: {report.counts.get(cat, 0)}")
    print(f"  Report written     : {report_path}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plinth — GitHub issue triage example",
    )
    parser.add_argument(
        "--repo",
        default="demo/repo",
        help="GitHub repo as 'owner/name'.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max number of issues to triage.",
    )
    parser.add_argument(
        "--mode",
        default="simulation",
        choices=["simulation", "live"],
        help="simulation = canned fixtures (no GitHub calls); live = real gateway.",
    )
    parser.add_argument(
        "--post-comments",
        action="store_true",
        help="Post a summary comment on each issue (live mode only).",
    )
    args = parser.parse_args(argv)

    if args.mode == "simulation":
        backend = SimulationBackend(args.repo, args.limit)
    else:
        if not gateway_available():
            print(
                "[triage] Plinth gateway and/or github-mcp are not reachable. "
                "Either start them or run with --mode=simulation."
            )
            return 2
        gateway = GatewayClient()
        backend = LiveBackend(args.repo, args.limit, gateway=gateway)

    if args.post_comments and args.mode == "simulation":
        print("[triage] --post-comments has no effect in simulation mode (skipping).")
        post = False
    else:
        post = args.post_comments

    report = run(
        repo=args.repo,
        mode=args.mode,
        limit=args.limit,
        post_comments=post,
        backend=backend,
    )
    path = write_report(report)
    _print_summary(report, path)
    # Echo the JSON summary for downstream tooling.
    print(
        "[triage] summary:",
        json.dumps({"repo": args.repo, "mode": args.mode, "counts": report.counts}),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
