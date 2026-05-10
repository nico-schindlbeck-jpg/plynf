/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * LLM-handler parity tests — show that a workflow step handler can
 * reach the LLM facade through `ctx.client.llm`, end-to-end, with
 * `MockProvider` so the suite runs offline.
 *
 * Mirrors the kind of coverage Python's `tests/test_handler_llm.py`
 * gives the reference worker. Anything that's load-bearing for an
 * application — non-streaming completion, streaming consumption,
 * error handling, multi-message conversations, audit-call wiring —
 * gets one focused test here.
 */
import { describe, expect, it, vi } from "vitest";

import {
  MockProvider,
  Plinth,
  type LLMProvider,
  type LLMProviderRequest,
  type LLMResponse,
  type LLMStreamChunk,
} from "@plinth/sdk";

import { WorkflowRuntime, type HandlerContext } from "../src/index.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a `Plinth` client whose `fetch` no-ops every request with a
 * 200 / empty JSON body. That swallows the audit-record POST without
 * having to spin up a full MockServer for tests that only care about
 * the LLM facade itself.
 */
function makeLLMOnlyClient(): { client: Plinth; fetchMock: ReturnType<typeof vi.fn> } {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify({ audit_id: "audit_test" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
  const client = new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    apiKey: "test-token",
    fetch: fetchMock as unknown as typeof fetch,
  });
  return { client, fetchMock };
}

/**
 * Build a minimal `HandlerContext`. Only the fields a handler reads
 * (`client`, `step.input`, `workerId`, `tools`) are populated; the
 * other slots are bare stubs so the cast through `unknown` is honest.
 */
function makeHandlerContext(opts: {
  client: Plinth;
  stepName?: string;
  stepInput?: unknown;
}): HandlerContext {
  return {
    client: opts.client,
    tools: opts.client.tools,
    step: {
      id: "step_test",
      name: opts.stepName ?? "extract",
      input: opts.stepInput ?? null,
    },
    workerId: "worker_test",
    workspace: undefined,
    workspaceRecord: undefined,
    workflow: undefined,
  } as unknown as HandlerContext;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("LLM-using handlers", () => {
  it("handler invokes client.llm.complete and returns the response content", async () => {
    const { client } = makeLLMOnlyClient();
    client.llm.useCustomProvider(
      new MockProvider({ responses: ["Solar power scaled rapidly..."] }),
    );

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "extract", async (ctx) => {
      const source = (ctx.step.input as { sourceText: string }).sourceText;
      const response = await ctx.client.llm.complete({
        model: "claude-sonnet-4-5",
        messages: [{ role: "user", content: `Extract facts from:\n${source}` }],
      });
      return {
        facts: response.content,
        tokens: response.inputTokens + response.outputTokens,
      };
    });

    const ctx = makeHandlerContext({
      client,
      stepInput: { sourceText: "Long source text..." },
    });
    const result = (await runtime.dispatch(
      "research-pipeline",
      "extract",
      ctx,
    )) as { facts: string; tokens: number };

    expect(result.facts).toBe("Solar power scaled rapidly...");
    expect(result.tokens).toEqual(expect.any(Number));
    expect(result.tokens).toBeGreaterThan(0);
  });

  it("handler that catches LLM errors returns a failure output instead of throwing", async () => {
    const { client } = makeLLMOnlyClient();

    // Provider that always throws — the handler is expected to catch
    // and translate this into a structured `{error}` payload rather
    // than letting the runtime mark the step `failed`.
    const erroringProvider: LLMProvider = {
      name: "always-fails",
      async complete(): Promise<LLMResponse> {
        throw new Error("upstream rate limit");
      },
      // eslint-disable-next-line require-yield
      async *stream(): AsyncGenerator<LLMStreamChunk, void, void> {
        throw new Error("upstream rate limit");
      },
      estimateCostUsd(): number {
        return 0;
      },
    };
    client.llm.useCustomProvider(erroringProvider);
    // Skip retry waits in tests.
    client.llm.configureRetries({ retries: 0, backoffSeconds: 0 });

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "extract", async (ctx) => {
      try {
        await ctx.client.llm.complete({
          model: "claude-sonnet-4-5",
          messages: [{ role: "user", content: "hi" }],
        });
        return { ok: true };
      } catch (err) {
        return { ok: false, error: (err as Error).message };
      }
    });

    const ctx = makeHandlerContext({ client });
    const result = (await runtime.dispatch(
      "research-pipeline",
      "extract",
      ctx,
    )) as { ok: boolean; error: string };

    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/rate limit/i);
  });

  it("handler streaming consumes all chunks before completing the step", async () => {
    const { client } = makeLLMOnlyClient();
    // chunkSize=4 splits the response into multiple deltas — verifies
    // the handler actually iterates instead of grabbing the first chunk.
    client.llm.useCustomProvider(
      new MockProvider({
        responses: ["streamed response payload"],
        chunkSize: 4,
      }),
    );

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "extract-stream", async (ctx) => {
      let accumulated = "";
      let chunkCount = 0;
      for await (const chunk of ctx.client.llm.stream({
        model: "mock-model",
        messages: [{ role: "user", content: "stream please" }],
      })) {
        accumulated += chunk.delta;
        chunkCount += 1;
      }
      return { content: accumulated, chunkCount };
    });

    const ctx = makeHandlerContext({ client, stepName: "extract-stream" });
    const result = (await runtime.dispatch(
      "research-pipeline",
      "extract-stream",
      ctx,
    )) as { content: string; chunkCount: number };

    expect(result.content).toBe("streamed response payload");
    // 24 chars / 4 + 1 terminal chunk = 7 chunks for the mock.
    expect(result.chunkCount).toBeGreaterThan(1);
  });

  it("handler passes a multi-message conversation to the provider verbatim", async () => {
    const { client } = makeLLMOnlyClient();
    // A capturing provider — records the request and returns a canned
    // response so we can assert exact message/role wiring downstream.
    const captured: LLMProviderRequest[] = [];
    const provider: LLMProvider = {
      name: "capture",
      async complete(req): Promise<LLMResponse> {
        captured.push(req);
        return {
          content: "captured-response",
          model: req.model,
          finishReason: "stop",
          inputTokens: 1,
          outputTokens: 1,
          costUsd: 0,
          durationMs: 1,
          provider: "capture",
          raw: {},
        };
      },
      // eslint-disable-next-line require-yield
      async *stream(): AsyncGenerator<LLMStreamChunk, void, void> {
        throw new Error("stream not used in this test");
      },
      estimateCostUsd(): number {
        return 0;
      },
    };
    client.llm.useCustomProvider(provider);

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "extract", async (ctx) => {
      const response = await ctx.client.llm.complete({
        model: "claude-sonnet-4-5",
        messages: [
          { role: "system", content: "You are a careful research assistant." },
          { role: "user", content: "What is the GDP of Switzerland?" },
        ],
        temperature: 0.2,
      });
      return { content: response.content };
    });

    const ctx = makeHandlerContext({ client });
    await runtime.dispatch("research-pipeline", "extract", ctx);

    expect(captured).toHaveLength(1);
    const req = captured[0]!;
    expect(req.model).toBe("claude-sonnet-4-5");
    expect(req.messages).toHaveLength(2);
    expect(req.messages[0]).toEqual({
      role: "system",
      content: "You are a careful research assistant.",
    });
    expect(req.messages[1]).toEqual({
      role: "user",
      content: "What is the GDP of Switzerland?",
    });
    expect(req.temperature).toBe(0.2);
  });

  it("MockProvider cycles through multiple responses across handler invocations", async () => {
    const { client } = makeLLMOnlyClient();
    client.llm.useCustomProvider(
      new MockProvider({ responses: ["alpha", "beta", "gamma"] }),
    );

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "extract", async (ctx) => {
      const response = await ctx.client.llm.complete({
        model: "claude-sonnet-4-5",
        messages: [
          {
            role: "user",
            content: (ctx.step.input as { prompt: string }).prompt,
          },
        ],
      });
      return { content: response.content };
    });

    const outputs: string[] = [];
    for (const prompt of ["one", "two", "three"]) {
      const ctx = makeHandlerContext({ client, stepInput: { prompt } });
      const result = (await runtime.dispatch(
        "research-pipeline",
        "extract",
        ctx,
      )) as { content: string };
      outputs.push(result.content);
    }
    expect(outputs).toEqual(["alpha", "beta", "gamma"]);
  });

  it("client.llm.complete records an audit POST to /v1/audit/record-llm", async () => {
    const { client, fetchMock } = makeLLMOnlyClient();
    client.llm.useCustomProvider(
      new MockProvider({ responses: ["audited reply"] }),
    );

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "extract", async (ctx) => {
      const response = await ctx.client.llm.complete({
        model: "claude-sonnet-4-5",
        messages: [{ role: "user", content: "audit me" }],
        workspaceId: "ws_audit",
      });
      return { content: response.content, auditId: response.auditId };
    });

    const ctx = makeHandlerContext({ client });
    const result = (await runtime.dispatch(
      "research-pipeline",
      "extract",
      ctx,
    )) as { content: string; auditId?: string };

    expect(result.content).toBe("audited reply");
    // Audit endpoint must have been hit exactly once.
    const auditCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/v1/audit/record-llm"),
    );
    expect(auditCalls).toHaveLength(1);
    const body = JSON.parse(auditCalls[0]![1].body as string) as Record<
      string,
      unknown
    >;
    expect(body.tool_id).toBe("llm.mock");
    expect(body.workspace_id).toBe("ws_audit");
    expect(typeof body.input_tokens).toBe("number");
    expect(typeof body.output_tokens).toBe("number");
    // The fake gateway echoes back `audit_id: "audit_test"`.
    expect(result.auditId).toBe("audit_test");
  });

  it("handler can read step input as the prompt and forward it to the LLM", async () => {
    const { client } = makeLLMOnlyClient();
    const captured: LLMProviderRequest[] = [];
    const provider: LLMProvider = {
      name: "capture",
      async complete(req): Promise<LLMResponse> {
        captured.push(req);
        return {
          content: "summary",
          model: req.model,
          finishReason: "stop",
          inputTokens: 0,
          outputTokens: 0,
          costUsd: 0,
          durationMs: 0,
          provider: "capture",
          raw: {},
        };
      },
      // eslint-disable-next-line require-yield
      async *stream(): AsyncGenerator<LLMStreamChunk, void, void> {
        throw new Error("not used");
      },
      estimateCostUsd(): number {
        return 0;
      },
    };
    client.llm.useCustomProvider(provider);

    const runtime = new WorkflowRuntime();
    runtime.register("research-pipeline", "summarise", async (ctx) => {
      const { prompt } = ctx.step.input as { prompt: string };
      const response = await ctx.client.llm.complete({
        model: "claude-sonnet-4-5",
        messages: [{ role: "user", content: prompt }],
      });
      return { content: response.content };
    });

    const ctx = makeHandlerContext({
      client,
      stepName: "summarise",
      stepInput: { prompt: "Summarise this in one sentence." },
    });
    await runtime.dispatch("research-pipeline", "summarise", ctx);

    expect(captured).toHaveLength(1);
    expect(captured[0]!.messages[0]!.content).toBe(
      "Summarise this in one sentence.",
    );
  });
});
