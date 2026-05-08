/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Workflow step handlers — TypeScript counterpart of `handlers.py`.
 *
 * The TS worker (`@plinth/workflow-worker`) imports this module and
 * calls `register(runtime, client)` to populate the dispatch table. The
 * registered keys must match the manifest in `start-workflow.ts`:
 *
 *   workflow:  research-pipeline
 *   steps:     search → fetch → extract → synth
 *
 * Each step writes its outputs to the workspace KV / files so the next
 * step can read them; snapshots are taken at every boundary so a crash
 * + worker restart resumes from the last known good state.
 */

import type { Plinth, JsonValue } from "@plinth/sdk";
import type { WorkflowRuntime } from "@plinth/workflow-worker";

import {
  mockExtract,
  mockFetch,
  mockSearch,
  mockSynthesise,
  slugify,
} from "./shared.js";

interface SearchInput {
  topic: string;
  k?: number;
}

interface MockSource {
  url: string;
  title: string;
  snippet: string;
}

export function register(runtime: WorkflowRuntime, _client: Plinth): void {
  runtime.register("research-pipeline", "search", async (ctx) => {
    const input = ctx.step.input as unknown as SearchInput;
    const sources = mockSearch(input.topic, input.k ?? 5);
    await ctx.workspace.kv.set("topic", input.topic);
    await ctx.workspace.kv.set(
      "sources/index",
      sources.map((s: MockSource) => s.url),
    );
    for (const s of sources) {
      await ctx.workspace.kv.set(`sources/meta/${s.url}`, s as unknown as JsonValue);
    }
    const snap = await ctx.workspace.snapshot(
      `after-search-${Math.floor(Date.now() / 1000)}`,
      { message: `search complete for ${JSON.stringify(input.topic)}` },
    );
    return { sources_count: sources.length, snapshot_id: snap.id };
  });

  runtime.register("research-pipeline", "fetch", async (ctx) => {
    const sourcesIdx = (await ctx.workspace.kv.get("sources/index")) as string[];
    let fetched = 0;
    for (const url of sourcesIdx) {
      const content = mockFetch(url);
      await ctx.workspace.files.write(`sources/${slugify(url)}.txt`, content);
      fetched += 1;
    }
    const snap = await ctx.workspace.snapshot(
      `after-fetch-${Math.floor(Date.now() / 1000)}`,
      { message: `fetched ${fetched} sources` },
    );
    return { fetched_count: fetched, snapshot_id: snap.id };
  });

  runtime.register("research-pipeline", "extract", async (ctx) => {
    const topic = (await ctx.workspace.kv.get("topic")) as string;
    const sourcesIdx = (await ctx.workspace.kv.get("sources/index")) as string[];
    let extracted = 0;
    for (const url of sourcesIdx) {
      const path = `sources/${slugify(url)}.txt`;
      let content: string;
      try {
        content = await ctx.workspace.files.readText(path);
      } catch {
        continue;
      }
      const facts = mockExtract(content, topic);
      await ctx.workspace.kv.set(`facts/${url}`, facts);
      extracted += 1;
    }
    const snap = await ctx.workspace.snapshot(
      `after-extract-${Math.floor(Date.now() / 1000)}`,
      { message: `extracted from ${extracted} sources` },
    );
    return { extracted_count: extracted, snapshot_id: snap.id };
  });

  runtime.register("research-pipeline", "synth", async (ctx) => {
    const topic = (await ctx.workspace.kv.get("topic")) as string;
    const sourcesIdx = (await ctx.workspace.kv.get("sources/index")) as string[];
    const factsByUrl: Record<string, string[]> = {};
    for (const url of sourcesIdx) {
      try {
        const facts = (await ctx.workspace.kv.get(`facts/${url}`)) as string[];
        if (facts) factsByUrl[url] = facts;
      } catch {
        continue;
      }
    }
    const report = mockSynthesise(topic, factsByUrl);
    await ctx.workspace.files.write("report.md", report);
    const snap = await ctx.workspace.snapshot(
      `after-synth-${Math.floor(Date.now() / 1000)}`,
      { message: "report written" },
    );
    return { report_chars: report.length, snapshot_id: snap.id };
  });
}
