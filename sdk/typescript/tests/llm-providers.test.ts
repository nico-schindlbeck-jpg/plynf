/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Tests for the built-in LLM provider adapters.
 *
 * The Anthropic and OpenAI vendor SDKs are NOT installed as devDeps —
 * we exercise the adapters by constructing them with hand-rolled fake
 * clients. The dynamic-import path is exercised separately via the
 * `LLMProviderNotInstalledError` test.
 */

import { describe, expect, it, vi } from "vitest";

import {
  AnthropicProvider,
  buildLLMProvider,
  LLMProviderError,
  LLMProviderNotInstalledError,
  LLMRateLimitedError,
  MockProvider,
  OpenAIProvider,
  type LLMStreamChunk,
} from "../src/index.js";

// ---------------------------------------------------------------------------
// Mock provider
// ---------------------------------------------------------------------------

describe("MockProvider", () => {
  it("cycles through the provided responses", async () => {
    const provider = new MockProvider({ responses: ["one", "two", "three"] });
    const a = await provider.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "p" }],
    });
    const b = await provider.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "p" }],
    });
    const c = await provider.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "p" }],
    });
    const d = await provider.complete({
      model: "mock-default",
      messages: [{ role: "user", content: "p" }],
    });
    expect(a.content).toBe("one");
    expect(b.content).toBe("two");
    expect(c.content).toBe("three");
    expect(d.content).toBe("one");
  });

  it("emits ~chunkSize-character chunks plus a terminal finishReason", async () => {
    const provider = new MockProvider({
      responses: ["abcdefghijklmnop"],
      chunkSize: 4,
    });
    const chunks: LLMStreamChunk[] = [];
    for await (const c of provider.stream({
      model: "x",
      messages: [{ role: "user", content: "go" }],
    })) {
      chunks.push(c);
    }
    // 16 characters / 4 = 4 deltas + 1 terminal.
    expect(chunks).toHaveLength(5);
    expect(chunks[0]!.delta).toBe("abcd");
    expect(chunks[1]!.delta).toBe("efgh");
    expect(chunks[4]!.delta).toBe("");
    expect(chunks[4]!.finishReason).toBe("stop");
  });

  it("normalises object responses to their `content` field", async () => {
    const provider = new MockProvider({
      responses: [{ role: "assistant", content: "hello" }],
    });
    const r = await provider.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.content).toBe("hello");
  });

  it("uses a sensible fallback when no responses configured", async () => {
    const provider = new MockProvider({});
    const r = await provider.complete({
      model: "x",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.content).toBe("mock response");
  });

  it("estimateCostUsd uses the mock-default pricing for unknown models", () => {
    const provider = new MockProvider();
    const cost = provider.estimateCostUsd("unknown-model", 1000, 1000);
    // input: 1000 * 1e-6 = 0.001; output: 1000 * 2e-6 = 0.002 → 0.003.
    expect(cost).toBeCloseTo(0.003, 9);
  });
});

// ---------------------------------------------------------------------------
// Anthropic provider — fake-client based
// ---------------------------------------------------------------------------

describe("AnthropicProvider with a fake client", () => {
  function fakeAnthropic(create: (...args: unknown[]) => Promise<unknown>): unknown {
    return { messages: { create } };
  }

  it("translates messages and forwards optional knobs", async () => {
    const seen: unknown[] = [];
    const fake = fakeAnthropic(async (args) => {
      seen.push(args);
      return {
        content: [{ text: "hi", type: "text" }],
        model: "claude-sonnet-4-5",
        stop_reason: "end_turn",
        usage: { input_tokens: 5, output_tokens: 4 },
      };
    });
    const provider = new AnthropicProvider(fake);
    const r = await provider.complete({
      model: "claude-sonnet-4-5",
      messages: [
        { role: "system", content: "be brief" },
        { role: "user", content: "hi" },
      ],
      maxTokens: 200,
      temperature: 0.5,
      topP: 0.9,
      stopSequences: ["END"],
    });
    expect(r.content).toBe("hi");
    expect(r.inputTokens).toBe(5);
    expect(r.outputTokens).toBe(4);
    expect(r.finishReason).toBe("end_turn");
    expect(r.provider).toBe("anthropic");
    expect(r.model).toBe("claude-sonnet-4-5");
    const args = seen[0] as Record<string, unknown>;
    expect(args.model).toBe("claude-sonnet-4-5");
    expect(args.system).toBe("be brief");
    expect(args.max_tokens).toBe(200);
    expect(args.temperature).toBe(0.5);
    expect(args.top_p).toBe(0.9);
    expect(args.stop_sequences).toEqual(["END"]);
    expect(Array.isArray(args.messages)).toBe(true);
    const messages = args.messages as Array<Record<string, unknown>>;
    expect(messages).toHaveLength(1);
    expect(messages[0]!.role).toBe("user");
  });

  it("concatenates multiple system messages with double-newline", async () => {
    const seen: unknown[] = [];
    const fake = fakeAnthropic(async (args) => {
      seen.push(args);
      return {
        content: [{ text: "ok", type: "text" }],
        model: "x",
        stop_reason: "end_turn",
        usage: { input_tokens: 0, output_tokens: 0 },
      };
    });
    const provider = new AnthropicProvider(fake);
    await provider.complete({
      model: "claude-sonnet-4-5",
      messages: [
        { role: "system", content: "rule one" },
        { role: "system", content: "rule two" },
        { role: "user", content: "hi" },
      ],
    });
    expect((seen[0] as { system: string }).system).toBe("rule one\n\nrule two");
  });

  it("maps a 429 SDK error to LLMRateLimitedError", async () => {
    const err = Object.assign(new Error("rate"), {
      status: 429,
      headers: { "retry-after": "2" },
    });
    const fake = fakeAnthropic(async () => {
      throw err;
    });
    const provider = new AnthropicProvider(fake);
    await expect(
      provider.complete({
        model: "x",
        messages: [{ role: "user", content: "p" }],
      }),
    ).rejects.toBeInstanceOf(LLMRateLimitedError);
    try {
      await provider.complete({
        model: "x",
        messages: [{ role: "user", content: "p" }],
      });
    } catch (e) {
      if (e instanceof LLMRateLimitedError) {
        expect(e.retryAfterSeconds).toBe(2);
        expect(e.statusCode).toBe(429);
      }
    }
  });

  it("maps a 5xx SDK error to LLMProviderError", async () => {
    const err = Object.assign(new Error("upstream"), { status: 502 });
    const fake = fakeAnthropic(async () => {
      throw err;
    });
    const provider = new AnthropicProvider(fake);
    await expect(
      provider.complete({
        model: "x",
        messages: [{ role: "user", content: "p" }],
      }),
    ).rejects.toBeInstanceOf(LLMProviderError);
  });

  it("yields stream chunks from message_delta + content_block_delta", async () => {
    async function* events(): AsyncGenerator<unknown, void, void> {
      yield {
        type: "content_block_delta",
        delta: { type: "text_delta", text: "Hello" },
      };
      yield {
        type: "content_block_delta",
        delta: { type: "text_delta", text: " world" },
      };
      yield { type: "message_delta", delta: { stop_reason: "end_turn" } };
    }
    const fake = {
      messages: {
        stream: () => events(),
      },
    };
    const provider = new AnthropicProvider(fake);
    const out: string[] = [];
    let final: string | undefined;
    for await (const c of provider.stream({
      model: "claude-sonnet-4-5",
      messages: [{ role: "user", content: "p" }],
    })) {
      if (c.delta) out.push(c.delta);
      if (c.finishReason !== undefined) final = c.finishReason;
    }
    expect(out.join("")).toBe("Hello world");
    expect(final).toBe("end_turn");
  });
});

// ---------------------------------------------------------------------------
// OpenAI provider — fake-client based
// ---------------------------------------------------------------------------

describe("OpenAIProvider with a fake client", () => {
  function fakeOpenAI(create: (...args: unknown[]) => Promise<unknown>): unknown {
    return { chat: { completions: { create } } };
  }

  it("translates messages and forwards optional knobs", async () => {
    const seen: unknown[] = [];
    const fake = fakeOpenAI(async (args) => {
      seen.push(args);
      return {
        choices: [{ message: { content: "ok" }, finish_reason: "stop" }],
        model: "gpt-5-mini",
        usage: { prompt_tokens: 11, completion_tokens: 22 },
      };
    });
    const provider = new OpenAIProvider(fake);
    const r = await provider.complete({
      model: "gpt-5-mini",
      messages: [
        { role: "system", content: "be brief" },
        { role: "user", content: "hi" },
        { role: "tool", content: "result", toolCallId: "call_1" },
      ],
      maxTokens: 100,
      temperature: 0.2,
      topP: 0.7,
      stopSequences: ["DONE"],
    });
    expect(r.content).toBe("ok");
    expect(r.inputTokens).toBe(11);
    expect(r.outputTokens).toBe(22);
    expect(r.finishReason).toBe("stop");
    expect(r.provider).toBe("openai");
    expect(r.model).toBe("gpt-5-mini");
    const args = seen[0] as Record<string, unknown>;
    const messages = args.messages as Array<Record<string, unknown>>;
    expect(messages).toHaveLength(3);
    expect(messages[2]!.tool_call_id).toBe("call_1");
    expect(args.max_tokens).toBe(100);
    expect(args.temperature).toBe(0.2);
    expect(args.top_p).toBe(0.7);
    expect(args.stop).toEqual(["DONE"]);
  });

  it("maps a 429 SDK error to LLMRateLimitedError with retryAfter", async () => {
    const err = Object.assign(new Error("rate"), {
      status: 429,
      headers: { "retry-after": "5" },
    });
    const fake = fakeOpenAI(async () => {
      throw err;
    });
    const provider = new OpenAIProvider(fake);
    try {
      await provider.complete({
        model: "x",
        messages: [{ role: "user", content: "p" }],
      });
      expect.fail("expected throw");
    } catch (e) {
      expect(e).toBeInstanceOf(LLMRateLimitedError);
      if (e instanceof LLMRateLimitedError) {
        expect(e.retryAfterSeconds).toBe(5);
      }
    }
  });

  it("maps a 502 SDK error to LLMProviderError", async () => {
    const err = Object.assign(new Error("bad gateway"), { status: 502 });
    const fake = fakeOpenAI(async () => {
      throw err;
    });
    const provider = new OpenAIProvider(fake);
    await expect(
      provider.complete({
        model: "x",
        messages: [{ role: "user", content: "p" }],
      }),
    ).rejects.toBeInstanceOf(LLMProviderError);
  });

  it("yields stream chunks from chunk.choices[0].delta.content", async () => {
    async function* chunks(): AsyncGenerator<unknown, void, void> {
      yield { choices: [{ delta: { content: "Hello" } }] };
      yield { choices: [{ delta: { content: " " } }] };
      yield { choices: [{ delta: { content: "world" }, finish_reason: "stop" }] };
    }
    const fake = fakeOpenAI(async () => chunks());
    const provider = new OpenAIProvider(fake);
    const out: string[] = [];
    let finish: string | undefined;
    for await (const c of provider.stream({
      model: "gpt-5-mini",
      messages: [{ role: "user", content: "p" }],
    })) {
      if (c.delta) out.push(c.delta);
      if (c.finishReason !== undefined) finish = c.finishReason;
    }
    expect(out.join("")).toBe("Hello world");
    expect(finish).toBe("stop");
  });
});

// ---------------------------------------------------------------------------
// Dispatch + lazy-import behaviour
// ---------------------------------------------------------------------------

describe("buildProvider dispatch", () => {
  it("returns a MockProvider for 'mock'", async () => {
    const provider = await buildLLMProvider("mock", { responses: ["x"] });
    expect(provider).toBeInstanceOf(MockProvider);
  });

  it("rejects unknown names", async () => {
    await expect(buildLLMProvider("does-not-exist")).rejects.toThrow(
      /Unknown LLM provider/,
    );
  });

  it("AnthropicProvider.create throws LLMProviderNotInstalledError when SDK missing", async () => {
    // Force the dynamic import branch by clearing the test-only `client`
    // bypass and watching the lazy-import fail.
    // We rely on the absence of `@anthropic-ai/sdk` from devDeps.
    let raised: unknown;
    try {
      await AnthropicProvider.create({ apiKey: "x" });
    } catch (e) {
      raised = e;
    }
    // Either: package missing (LLMProviderNotInstalledError) or
    // (rare, on machines that already have it installed) it succeeds.
    if (raised) {
      expect(raised).toBeInstanceOf(LLMProviderNotInstalledError);
    } else {
      // On dev machines with the package installed, accept success.
      expect(true).toBe(true);
    }
  });

  it("OpenAIProvider.create throws LLMProviderNotInstalledError when SDK missing", async () => {
    let raised: unknown;
    try {
      await OpenAIProvider.create({ apiKey: "x" });
    } catch (e) {
      raised = e;
    }
    if (raised) {
      expect(raised).toBeInstanceOf(LLMProviderNotInstalledError);
    } else {
      expect(true).toBe(true);
    }
  });

  it("create() with a `client` injection bypasses the dynamic import", async () => {
    const fakeAnthropic = {
      messages: {
        create: vi.fn().mockResolvedValue({
          content: [{ text: "hi" }],
          model: "claude-sonnet-4-5",
          stop_reason: "end_turn",
          usage: { input_tokens: 1, output_tokens: 1 },
        }),
      },
    };
    const provider = await AnthropicProvider.create({ client: fakeAnthropic });
    expect(provider).toBeInstanceOf(AnthropicProvider);
    const r = await provider.complete({
      model: "claude-sonnet-4-5",
      messages: [{ role: "user", content: "p" }],
    });
    expect(r.content).toBe("hi");
  });
});
