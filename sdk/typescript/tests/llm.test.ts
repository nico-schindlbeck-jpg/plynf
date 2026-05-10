/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Tests for the v1.2 LLM client surface (`src/llm.ts`).
 *
 * Coverage:
 *   * Provider configuration (`useProvider`, `useCustomProvider`)
 *   * Auto-detection from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
 *   * `complete()` happy path against MockProvider
 *   * `stream()` yields chunks; totals match `complete`
 *   * Retry on 429 (honours `retryAfterSeconds`)
 *   * Retry on 5xx with exponential back-off
 *   * No retry on 4xx-other
 *   * `LLMRetryExhaustedError` after max attempts
 *   * Audit POST shape + auditId propagation
 *   * Audit failure does not break the call
 *   * `LLMProviderNotConfiguredError` when no provider
 *   * Cost integration with vendor pricing tables
 *
 * Vendor SDK availability is irrelevant — providers are tested via
 * direct construction with fake clients.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AnthropicProvider,
  ANTHROPIC_PRICING,
  countTokens,
  LLMClient,
  LLMError,
  LLMProviderError,
  LLMProviderNotConfiguredError,
  LLMRateLimitedError,
  LLMRetryExhaustedError,
  MockProvider,
  MOCK_PRICING,
  OpenAIProvider,
  OPENAI_PRICING,
  Plinth,
  type LLMProvider,
  type LLMProviderRequest,
  type LLMResponse,
  type LLMStreamChunk,
} from "../src/index.js";
import { MockServer } from "./_helpers.js";

function makeClient(server: MockServer): Plinth {
  return new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    apiKey: "test-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
}

/** Drive the retry loop synchronously — no real waits. */
function disableSleep(client: Plinth): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (client.llm as any)._setSleepForTests(async () => {});
}

describe("LLMClient — wiring + provider configuration", () => {
  it("client.llm is attached and starts with no provider", () => {
    const server = new MockServer();
    const client = makeClient(server);
    expect(client.llm).toBeInstanceOf(LLMClient);
    expect(client.llm.getProvider()).toBeNull();
  });

  it("useProvider('mock') returns the provider and sets it as active", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    const provider = await client.llm.useProvider("mock", { responses: ["hello"] });
    expect(provider).toBeInstanceOf(MockProvider);
    expect(client.llm.getProvider()).toBe(provider);
  });

  it("useProvider('unknown') throws", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    await expect(client.llm.useProvider("nope")).rejects.toThrow(/Unknown LLM provider/);
  });

  it("useCustomProvider replaces the active provider", () => {
    const server = new MockServer();
    const client = makeClient(server);
    const custom = new MockProvider({ responses: ["x"] });
    client.llm.useCustomProvider(custom);
    expect(client.llm.getProvider()).toBe(custom);
  });

  it("complete without a provider throws LLMProviderNotConfiguredError", async () => {
    const prevA = process.env.ANTHROPIC_API_KEY;
    const prevO = process.env.OPENAI_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.OPENAI_API_KEY;
    try {
      const server = new MockServer();
      const client = makeClient(server);
      await expect(
        client.llm.complete({
          model: "x",
          messages: [{ role: "user", content: "hi" }],
        }),
      ).rejects.toBeInstanceOf(LLMProviderNotConfiguredError);
    } finally {
      if (prevA !== undefined) process.env.ANTHROPIC_API_KEY = prevA;
      if (prevO !== undefined) process.env.OPENAI_API_KEY = prevO;
    }
  });
});

describe("LLMClient — auto-detection from env", () => {
  let prevA: string | undefined;
  let prevO: string | undefined;

  beforeEach(() => {
    prevA = process.env.ANTHROPIC_API_KEY;
    prevO = process.env.OPENAI_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.OPENAI_API_KEY;
  });
  afterEach(() => {
    if (prevA !== undefined) process.env.ANTHROPIC_API_KEY = prevA;
    else delete process.env.ANTHROPIC_API_KEY;
    if (prevO !== undefined) process.env.OPENAI_API_KEY = prevO;
    else delete process.env.OPENAI_API_KEY;
  });

  it("auto-detects Anthropic from ANTHROPIC_API_KEY", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-ant-test";
    // Stub the constructor path so we don't need the real package.
    const fakeClient = { messages: { create: vi.fn() } };
    vi.spyOn(AnthropicProvider, "create").mockResolvedValue(
      new AnthropicProvider(fakeClient),
    );
    const server = new MockServer();
    const client = makeClient(server);
    // We don't actually call complete() (would hit a real API);
    // a getProvider() probe through a stream is enough — we just
    // need ensureProvider() to fire.
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    // Use a fake response by mocking client.messages.create.
    (fakeClient.messages.create as ReturnType<typeof vi.fn>).mockResolvedValue({
      content: [{ text: "hi" }],
      model: "claude-sonnet-4-5",
      stop_reason: "end_turn",
      usage: { input_tokens: 1, output_tokens: 1 },
    });
    const r = await client.llm.complete({
      model: "claude-sonnet-4-5",
      messages: [{ role: "user", content: "ping" }],
    });
    expect(r.provider).toBe("anthropic");
    expect(r.content).toBe("hi");
  });

  it("auto-detects OpenAI from OPENAI_API_KEY when Anthropic is unset", async () => {
    process.env.OPENAI_API_KEY = "sk-test";
    const fakeClient = { chat: { completions: { create: vi.fn() } } };
    vi.spyOn(OpenAIProvider, "create").mockResolvedValue(
      new OpenAIProvider(fakeClient),
    );
    const server = new MockServer();
    const client = makeClient(server);
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    (fakeClient.chat.completions.create as ReturnType<typeof vi.fn>).mockResolvedValue({
      choices: [{ message: { content: "hi" }, finish_reason: "stop" }],
      model: "gpt-5-mini",
      usage: { prompt_tokens: 1, completion_tokens: 1 },
    });
    const r = await client.llm.complete({
      model: "gpt-5-mini",
      messages: [{ role: "user", content: "ping" }],
    });
    expect(r.provider).toBe("openai");
    expect(r.content).toBe("hi");
  });

  it("Anthropic wins on a tie (both env vars set)", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-ant";
    process.env.OPENAI_API_KEY = "sk-oai";
    const fakeAnthropicCreate = vi.fn();
    const fakeAnthropic = { messages: { create: fakeAnthropicCreate } };
    vi.spyOn(AnthropicProvider, "create").mockResolvedValue(
      new AnthropicProvider(fakeAnthropic),
    );
    fakeAnthropicCreate.mockResolvedValue({
      content: [{ text: "ah" }],
      model: "x",
      stop_reason: "end_turn",
      usage: { input_tokens: 0, output_tokens: 0 },
    });
    const server = new MockServer();
    const client = makeClient(server);
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "y" }, 201);
    const r = await client.llm.complete({
      model: "claude-sonnet-4-5",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.provider).toBe("anthropic");
  });
});

describe("LLMClient.complete — happy path", () => {
  it("returns the expected response shape from MockProvider", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "audit-1" }, 201);
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["abc def ghi"] });

    const response = await client.llm.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "hello world" }],
    });
    expect(response.content).toBe("abc def ghi");
    expect(response.model).toBe("mock-default");
    expect(response.finishReason).toBe("stop");
    expect(response.provider).toBe("mock");
    expect(response.inputTokens).toBeGreaterThan(0);
    expect(response.outputTokens).toBeGreaterThan(0);
    expect(response.costUsd).toBeGreaterThan(0);
    expect(response.durationMs).toBeGreaterThanOrEqual(0);
    expect(response.auditId).toBe("audit-1");
  });

  it("uses the next response on subsequent calls", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["one", "two"] });
    const a = await client.llm.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    const b = await client.llm.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(a.content).toBe("one");
    expect(b.content).toBe("two");
  });

  it("forwards optional knobs to the provider", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    const client = makeClient(server);
    const captured: LLMProviderRequest[] = [];
    const recording: LLMProvider = {
      name: "mock",
      async complete(req) {
        captured.push(req);
        return {
          content: "ok",
          model: req.model,
          finishReason: "stop",
          inputTokens: 0,
          outputTokens: 0,
          costUsd: 0,
          durationMs: 0,
          provider: "mock",
          raw: {},
        };
      },
      async *stream() {
        yield { delta: "", finishReason: "stop", raw: {} } satisfies LLMStreamChunk;
      },
      estimateCostUsd: () => 0,
    };
    client.llm.useCustomProvider(recording);
    await client.llm.complete({
      model: "m",
      messages: [{ role: "user", content: "x" }],
      maxTokens: 256,
      temperature: 0.5,
      topP: 0.8,
      stopSequences: ["END"],
      extra: { custom_arg: 1 },
    });
    expect(captured).toHaveLength(1);
    expect(captured[0]!.maxTokens).toBe(256);
    expect(captured[0]!.temperature).toBe(0.5);
    expect(captured[0]!.topP).toBe(0.8);
    expect(captured[0]!.stopSequences).toEqual(["END"]);
    expect(captured[0]!.extra).toEqual({ custom_arg: 1 });
  });
});

describe("LLMClient.stream", () => {
  it("yields delta chunks plus a terminal finishReason chunk", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "stream-1" }, 201);
    const client = makeClient(server);
    await client.llm.useProvider("mock", {
      responses: ["alpha beta gamma delta"],
      chunkSize: 4,
    });

    const chunks: LLMStreamChunk[] = [];
    for await (const c of client.llm.stream({
      model: "x",
      messages: [{ role: "user", content: "go" }],
    })) {
      chunks.push(c);
    }
    expect(chunks.length).toBeGreaterThan(1);
    const text = chunks.map((c) => c.delta).join("");
    expect(text).toBe("alpha beta gamma delta");
    const last = chunks.at(-1)!;
    expect(last.finishReason).toBe("stop");
  });

  it("records audit after the iterator drains", async () => {
    const server = new MockServer();
    let auditBody: Record<string, unknown> | null = null;
    server.on("POST", /\/v1\/audit\/record-llm/, (req) => {
      auditBody = JSON.parse(req.body ?? "{}");
      return { status: 201, body: { audit_id: "stream-audit" } };
    });
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["hello world"] });
    let count = 0;
    for await (const _c of client.llm.stream({
      model: "mock-default",
      messages: [{ role: "user", content: "go" }],
    })) {
      count++;
    }
    expect(count).toBeGreaterThan(0);
    expect(auditBody).not.toBeNull();
    expect(auditBody!.tool_id).toBe("llm.mock");
    expect(auditBody!.model).toBe("mock-default");
    expect(typeof auditBody!.input_tokens).toBe("number");
    expect(typeof auditBody!.output_tokens).toBe("number");
    expect(typeof auditBody!.cost_usd).toBe("number");
  });

  it("stream + complete produce the same total content", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["streamed-content-here"] });
    const completion = await client.llm.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(completion.content).toBe("streamed-content-here");

    await client.llm.useProvider("mock", { responses: ["streamed-content-here"] });
    let streamed = "";
    for await (const c of client.llm.stream({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    })) {
      streamed += c.delta;
    }
    expect(streamed).toBe(completion.content);
  });
});

// ---------------------------------------------------------------------------
// Retry loop
// ---------------------------------------------------------------------------

/** Provider that throws scripted errors for the first N calls, then succeeds. */
class FlakeyProvider implements LLMProvider {
  readonly name = "flakey";
  readonly calls: LLMProviderRequest[] = [];
  constructor(
    private readonly errors: Error[],
    private readonly success: LLMResponse,
  ) {}
  async complete(req: LLMProviderRequest): Promise<LLMResponse> {
    this.calls.push(req);
    if (this.calls.length <= this.errors.length) {
      throw this.errors[this.calls.length - 1]!;
    }
    return { ...this.success };
  }
  async *stream(_req: LLMProviderRequest): AsyncGenerator<LLMStreamChunk, void, void> {
    yield { delta: this.success.content, raw: {} };
    yield { delta: "", finishReason: "stop", raw: {} };
  }
  estimateCostUsd(): number {
    return 0;
  }
}

const successResponse: LLMResponse = {
  content: "ok",
  model: "m",
  finishReason: "stop",
  inputTokens: 1,
  outputTokens: 1,
  costUsd: 0.0,
  durationMs: 0,
  provider: "flakey",
  raw: {},
};

describe("LLMClient retry loop", () => {
  it("retries on 429 honouring retryAfterSeconds", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    const client = makeClient(server);
    disableSleep(client);
    const provider = new FlakeyProvider(
      [
        new LLMRateLimitedError("rate limited", { retryAfterSeconds: 0.1, provider: "flakey" }),
      ],
      successResponse,
    );
    client.llm.useCustomProvider(provider);
    const r = await client.llm.complete({
      model: "m",
      messages: [{ role: "user", content: "x" }],
    });
    expect(r.content).toBe("ok");
    expect(provider.calls).toHaveLength(2);
  });

  it("retries on 5xx", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    const client = makeClient(server);
    disableSleep(client);
    const provider = new FlakeyProvider(
      [
        new LLMProviderError("upstream", { statusCode: 503, provider: "flakey" }),
      ],
      successResponse,
    );
    client.llm.useCustomProvider(provider);
    const r = await client.llm.complete({
      model: "m",
      messages: [{ role: "user", content: "x" }],
    });
    expect(r.content).toBe("ok");
    expect(provider.calls).toHaveLength(2);
  });

  it("does NOT retry on 4xx-other (e.g. 400)", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    disableSleep(client);
    const provider = new FlakeyProvider(
      [
        new LLMProviderError("bad request", { statusCode: 400, provider: "flakey" }),
      ],
      successResponse,
    );
    client.llm.useCustomProvider(provider);
    await expect(
      client.llm.complete({ model: "m", messages: [{ role: "user", content: "x" }] }),
    ).rejects.toBeInstanceOf(LLMProviderError);
    expect(provider.calls).toHaveLength(1);
  });

  it("throws LLMRetryExhaustedError after the budget is gone", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    disableSleep(client);
    client.llm.configureRetries({ retries: 2, backoffSeconds: 0 });
    // Three errors → 1 initial + 2 retries = 3 attempts, all fail.
    const provider = new FlakeyProvider(
      [
        new LLMRateLimitedError("rl", { retryAfterSeconds: 0, provider: "flakey" }),
        new LLMRateLimitedError("rl", { retryAfterSeconds: 0, provider: "flakey" }),
        new LLMRateLimitedError("rl", { retryAfterSeconds: 0, provider: "flakey" }),
      ],
      successResponse,
    );
    client.llm.useCustomProvider(provider);
    await expect(
      client.llm.complete({ model: "m", messages: [{ role: "user", content: "x" }] }),
    ).rejects.toBeInstanceOf(LLMRetryExhaustedError);
    expect(provider.calls).toHaveLength(3);
  });

  it("falls back to exponential backoff when retryAfterSeconds is null", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "x" }, 201);
    const client = makeClient(server);
    const sleeps: number[] = [];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (client.llm as any)._setSleepForTests(async (ms: number) => {
      sleeps.push(ms);
    });
    client.llm.configureRetries({ retries: 3, backoffSeconds: 0.5 });
    const provider = new FlakeyProvider(
      [
        new LLMRateLimitedError("rl", { provider: "flakey" }), // no retryAfter
        new LLMRateLimitedError("rl", { provider: "flakey" }),
      ],
      successResponse,
    );
    client.llm.useCustomProvider(provider);
    await client.llm.complete({
      model: "m",
      messages: [{ role: "user", content: "x" }],
    });
    expect(sleeps).toEqual([500, 1000]); // 0.5s, 1s
  });

  it("retries are configurable via configureRetries", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    disableSleep(client);
    client.llm.configureRetries({ retries: 0 });
    const provider = new FlakeyProvider(
      [new LLMRateLimitedError("rl", { provider: "flakey" })],
      successResponse,
    );
    client.llm.useCustomProvider(provider);
    await expect(
      client.llm.complete({ model: "m", messages: [{ role: "user", content: "x" }] }),
    ).rejects.toBeInstanceOf(LLMRetryExhaustedError);
    expect(provider.calls).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Audit recording
// ---------------------------------------------------------------------------

describe("LLMClient audit POST", () => {
  it("records the audit POST with the expected snake_case shape", async () => {
    const server = new MockServer();
    let body: Record<string, unknown> | null = null;
    let headers: Record<string, string> = {};
    server.on("POST", /\/v1\/audit\/record-llm/, (req) => {
      body = JSON.parse(req.body ?? "{}");
      headers = req.headers;
      return { status: 201, body: { audit_id: "audit-123" } };
    });
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["hi"] });
    await client.llm.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "test" }],
      workspaceId: "ws-1",
      agentId: "agent-1",
    });
    expect(body).not.toBeNull();
    expect(body!.tool_id).toBe("llm.mock");
    expect(body!.model).toBe("mock-default");
    expect(body!.workspace_id).toBe("ws-1");
    expect(body!.agent_id).toBe("agent-1");
    expect(body!.finish_reason).toBe("stop");
    expect(typeof body!.input_tokens).toBe("number");
    expect(typeof body!.output_tokens).toBe("number");
    expect(typeof body!.cost_usd).toBe("number");
    expect(typeof body!.duration_ms).toBe("number");
    expect(headers.authorization?.toLowerCase()).toBe("bearer test-token");
  });

  it("sets response.auditId from the audit POST response", async () => {
    const server = new MockServer();
    server.json("POST", /\/v1\/audit\/record-llm/, { audit_id: "the-audit" }, 201);
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["x"] });
    const r = await client.llm.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.auditId).toBe("the-audit");
  });

  it("audit failure does not break the call", async () => {
    const server = new MockServer();
    // Server returns 500 on audit POST.
    server.on("POST", /\/v1\/audit\/record-llm/, () => ({
      status: 500,
      body: { error: { code: "INTERNAL", message: "boom" } },
    }));
    const client = makeClient(server);
    await client.llm.useProvider("mock", { responses: ["recovered"] });
    const r = await client.llm.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.content).toBe("recovered");
    expect(r.auditId).toBeUndefined();
  });

  it("audit fetch throwing does not surface as an error", async () => {
    // Stub fetch to *throw* on the audit endpoint specifically.
    const baseFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof URL ? input.toString() : String(input);
      if (url.includes("/v1/audit/record-llm")) throw new Error("network down");
      return new Response(null, { status: 200 });
    });
    const client = new Plinth({
      workspaceUrl: "http://workspace.test",
      gatewayUrl: "http://gateway.test",
      apiKey: "test-token",
      fetch: baseFetch as unknown as typeof fetch,
    });
    await client.llm.useProvider("mock", { responses: ["safe"] });
    const r = await client.llm.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.content).toBe("safe");
    expect(r.auditId).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Cost calculations
// ---------------------------------------------------------------------------

describe("Cost tables and estimateCostUsd", () => {
  it("Anthropic pricing matches the published Sonnet rates", () => {
    const sonnet = ANTHROPIC_PRICING["claude-sonnet-4-5"]!;
    expect(sonnet.input).toBeCloseTo(3 / 1_000_000, 12);
    expect(sonnet.output).toBeCloseTo(15 / 1_000_000, 12);
  });

  it("OpenAI pricing matches the published gpt-5 rates", () => {
    const five = OPENAI_PRICING["gpt-5"]!;
    expect(five.input).toBeCloseTo(1.25 / 1_000_000, 12);
    expect(five.output).toBeCloseTo(10.0 / 1_000_000, 12);
  });

  it("MockProvider applies its own pricing table", async () => {
    const provider = new MockProvider({ responses: ["hello world"] });
    const cost = provider.estimateCostUsd("mock-default", 1_000_000, 0);
    expect(cost).toBeCloseTo(MOCK_PRICING["mock-default"]!.input * 1_000_000, 9);
    expect(cost).toBeCloseTo(1.0, 9);
  });

  it("Unknown model in Anthropic falls back to Sonnet pricing", async () => {
    const fakeClient = {};
    const provider = new AnthropicProvider(fakeClient);
    const cost = provider.estimateCostUsd("claude-something-else", 100, 200);
    expect(cost).toBeCloseTo(
      100 * (3 / 1_000_000) + 200 * (15 / 1_000_000),
      12,
    );
  });

  it("Unknown model in OpenAI falls back to gpt-5-mini pricing", async () => {
    const provider = new OpenAIProvider({});
    const cost = provider.estimateCostUsd("gpt-future", 100, 200);
    expect(cost).toBeCloseTo(
      100 * (0.25 / 1_000_000) + 200 * (2.0 / 1_000_000),
      12,
    );
  });
});

// ---------------------------------------------------------------------------
// Token counting parity
// ---------------------------------------------------------------------------

describe("Token counting", () => {
  it("MockProvider's token counts agree with countTokens", async () => {
    const provider = new MockProvider({ responses: ["the quick brown fox"] });
    const r = await provider.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "input goes here" }],
    });
    const inExpected = await countTokens("input goes here");
    const outExpected = await countTokens("the quick brown fox");
    expect(r.inputTokens).toBe(inExpected);
    expect(r.outputTokens).toBe(outExpected);
  });

  it("LLMError is the base class for all LLM errors", () => {
    expect(new LLMRateLimitedError("x") instanceof LLMError).toBe(true);
    expect(new LLMProviderError("x") instanceof LLMError).toBe(true);
    expect(new LLMRetryExhaustedError("x", 1) instanceof LLMError).toBe(true);
    expect(new LLMProviderNotConfiguredError("x") instanceof LLMError).toBe(true);
  });
});
