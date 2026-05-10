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
 *
 * `extract` calls the LLM facade via `ctx.client.llm.complete(...)`. By
 * default the example wires `MockProvider` (offline, deterministic) so
 * the workflow runs without an API key — flip to Anthropic or OpenAI
 * in `start-workflow.ts` once you've exported the relevant env var.
 */

import { MockProvider, type Plinth, type JsonValue } from "@plinth/sdk";
import type { WorkflowRuntime } from "@plinth/workflow-worker";

import {
  mockFetch,
  mockSearch,
  mockSynthesise,
  slugify,
} from "./shared.js";

/**
 * Wire an LLM provider before the first handler runs.
 *
 *   * `ANTHROPIC_API_KEY` → AnthropicProvider (real Claude calls).
 *   * `OPENAI_API_KEY`   → OpenAIProvider (real GPT calls).
 *   * otherwise           → MockProvider (deterministic, offline).
 *
 * The auto-detection inside `LLMClient` would also resolve env keys at
 * the first `complete()` call, but configuring eagerly here makes the
 * worker boot fail loud — and gives us a clean place to install the
 * offline mock when no key is set.
 */
async function configureLLM(client: Plinth): Promise<void> {
  if (process.env.ANTHROPIC_API_KEY) {
    await client.llm.useProvider("anthropic", {
      apiKey: process.env.ANTHROPIC_API_KEY,
    });
    return;
  }
  if (process.env.OPENAI_API_KEY) {
    await client.llm.useProvider("openai", {
      apiKey: process.env.OPENAI_API_KEY,
    });
    return;
  }
  // Offline default — same shape and timing as a real provider, no
  // network calls. Useful for `make demo-durable-ts` runs without an
  // API key in the environment.
  client.llm.useCustomProvider(
    new MockProvider({
      responses: [
        "- Mock fact one\n- Mock fact two\n- Mock fact three",
      ],
      defaultModel: "claude-sonnet-4-5",
    }),
  );
}

interface SearchInput {
  topic: string;
  k?: number;
}

interface MockSource {
  url: string;
  title: string;
  snippet: string;
}

export async function register(runtime: WorkflowRuntime, client: Plinth): Promise<void> {
  await configureLLM(client);

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

  // `extract` uses the LLM facade (`ctx.client.llm`) to pull short
  // fact lists from each fetched source. Provider configuration lives
  // in `start-workflow.ts` so the handler stays portable across mock
  // and real providers.
  runtime.register("research-pipeline", "extract", async (ctx) => {
    const topic = (await ctx.workspace.kv.get("topic")) as string;
    const sourcesIdx = (await ctx.workspace.kv.get("sources/index")) as string[];
    const stepInput = (ctx.step.input ?? {}) as { model?: string };
    const model = stepInput.model ?? "claude-sonnet-4-5";

    let extracted = 0;
    let totalInputTokens = 0;
    let totalOutputTokens = 0;
    let totalCostUsd = 0;

    for (const url of sourcesIdx) {
      const path = `sources/${slugify(url)}.txt`;
      let content: string;
      try {
        content = await ctx.workspace.files.readText(path);
      } catch {
        continue;
      }

      const response = await ctx.client.llm.complete({
        model,
        messages: [
          {
            role: "system",
            content:
              "You are a careful research assistant. Extract 3-5 short, " +
              "verifiable facts as a bulleted list. No commentary.",
          },
          {
            role: "user",
            content: `Topic: ${topic}\nSource (${url}):\n${content}`,
          },
        ],
        workspaceId: ctx.workspaceRecord?.id,
      });

      // Persist the raw LLM output so the synth step can read it. The
      // existing synth handler expects `string[]` — we wrap the
      // response in a one-element array to keep the chain working
      // without coupling synth to LLM-shaped output.
      await ctx.workspace.kv.set(`facts/${url}`, [response.content] as unknown as JsonValue);
      totalInputTokens += response.inputTokens;
      totalOutputTokens += response.outputTokens;
      totalCostUsd += response.costUsd;
      extracted += 1;
    }

    const snap = await ctx.workspace.snapshot(
      `after-extract-${Math.floor(Date.now() / 1000)}`,
      { message: `extracted from ${extracted} sources via ${model}` },
    );
    return {
      extracted_count: extracted,
      input_tokens: totalInputTokens,
      output_tokens: totalOutputTokens,
      cost_usd: totalCostUsd,
      model,
      snapshot_id: snap.id,
    };
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
