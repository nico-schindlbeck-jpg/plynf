# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workflow step handlers for the durable-workflow example.

The worker process imports this module on startup. The decorations
populate ``client._workflow_runtime`` so the worker's dispatch table
matches the manifest the start script creates.

Workflow:  ``research-pipeline``
Steps:     ``search`` → ``fetch`` → ``extract`` → ``synth``

Each step writes its outputs to the workspace KV / files so the next
step can read them. Snapshots are taken at every boundary so a
crash + worker restart can resume from the last known good state
without redoing work.
"""

from __future__ import annotations

import time

from plinth import Plinth

from shared import (
    make_client_kwargs,
    mock_extract,
    mock_fetch,
    mock_search,
    mock_synthesise,
    slugify,
)

# ---------------------------------------------------------------------------
# Client + handler registrations
# ---------------------------------------------------------------------------
#
# Both this handlers module AND ``start_workflow.py`` construct a
# ``Plinth`` client; that's fine — they're separate processes. The worker
# loader (``__main__.py`` in plinth-workflow-worker) re-uses *this*
# module's client instance (and therefore its runtime registry) by
# scanning the imported module for ``Plinth`` attributes.

client = Plinth(**make_client_kwargs())


@client.workflow_handler("research-pipeline", step="search")
def search_step(ctx) -> dict:
    """Find ``k`` candidate sources for the input topic."""
    topic = ctx.step.input["topic"]
    sources = mock_search(topic, k=ctx.step.input.get("k", 5))
    ctx.workspace.kv.set("topic", topic)
    ctx.workspace.kv.set("sources/index", [s["url"] for s in sources])
    for s in sources:
        ctx.workspace.kv.set(f"sources/meta/{s['url']}", s)
    # Snapshot: worker will reference this snapshot in the step row so
    # a downstream resumer knows where to restore from.
    snap = ctx.workspace.snapshot(
        f"after-search-{int(time.time())}",
        message=f"search complete for {topic!r}",
    )
    return {"sources_count": len(sources), "snapshot_id": snap.id}


@client.workflow_handler("research-pipeline", step="fetch")
def fetch_step(ctx) -> dict:
    """Fetch each source's content and write to versioned files."""
    sources_idx = ctx.workspace.kv.get("sources/index")
    fetched = 0
    for url in sources_idx:
        content = mock_fetch(url)
        ctx.workspace.files.write(f"sources/{slugify(url)}.txt", content)
        fetched += 1
    snap = ctx.workspace.snapshot(
        f"after-fetch-{int(time.time())}",
        message=f"fetched {fetched} sources",
    )
    return {"fetched_count": fetched, "snapshot_id": snap.id}


@client.workflow_handler("research-pipeline", step="extract")
def extract_step(ctx) -> dict:
    """Extract facts from each fetched source."""
    topic = ctx.workspace.kv.get("topic")
    sources_idx = ctx.workspace.kv.get("sources/index")
    extracted = 0
    for url in sources_idx:
        path = f"sources/{slugify(url)}.txt"
        try:
            content = ctx.workspace.files.read(path, as_text=True)
        except Exception:  # noqa: BLE001 — file race; skip
            continue
        facts = mock_extract(content, topic=topic)
        ctx.workspace.kv.set(f"facts/{url}", facts)
        extracted += 1
    snap = ctx.workspace.snapshot(
        f"after-extract-{int(time.time())}",
        message=f"extracted from {extracted} sources",
    )
    return {"extracted_count": extracted, "snapshot_id": snap.id}


@client.workflow_handler("research-pipeline", step="synth")
def synth_step(ctx) -> dict:
    """Synthesise a final report from per-source facts."""
    topic = ctx.workspace.kv.get("topic")
    sources_idx = ctx.workspace.kv.get("sources/index")
    facts_by_url: dict[str, list[str]] = {}
    for url in sources_idx:
        try:
            facts_by_url[url] = ctx.workspace.kv.get(f"facts/{url}")
        except Exception:  # noqa: BLE001
            continue
    report = mock_synthesise(topic, facts_by_url)
    ctx.workspace.files.write("report.md", report)
    snap = ctx.workspace.snapshot(
        f"after-synth-{int(time.time())}",
        message="report written",
    )
    return {"report_chars": len(report), "snapshot_id": snap.id}


__all__ = ["client"]
