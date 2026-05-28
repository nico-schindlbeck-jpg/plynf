/**
 * SPDX-License-Identifier: Apache-2.0
 * Tests for the Plynf proxy client (TypeScript SDK).
 */

import { describe, it, expect, vi } from "vitest";
import {
  PlynfOpenAI,
  PlynfProxyError,
  wrapTool,
  wrapTools,
} from "../src/proxy-client/index.js";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("PlynfOpenAI drop-in", () => {
  it("routes chat.completions.create to /v1/chat/completions on Plynf", async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      expect(String(url)).toMatch(/\/v1\/chat\/completions$/);
      const body = JSON.parse((init?.body as string) ?? "{}");
      expect(body.model).toBe("gpt-4o");
      expect(body.stream).toBe(false);
      expect(init?.headers as Record<string, string>).toMatchObject({
        Authorization: "Bearer sk-test",
      });
      return jsonResponse(200, {
        id: "chatcmpl-1",
        object: "chat.completion",
        created: 1,
        model: "gpt-4o",
        choices: [
          {
            index: 0,
            finish_reason: "stop",
            message: { role: "assistant", content: "ok" },
          },
        ],
      });
    });

    const client = new PlynfOpenAI({
      apiKey: "sk-test",
      plynfUrl: "https://plynf.test",
      fetch: fetchMock as unknown as typeof fetch,
    });
    const resp = await client.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "user", content: "hi" }],
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    // Non-streaming returns the response body shape directly.
    expect((resp as { choices: { message: { content: string } }[] }).choices[0].message.content).toBe(
      "ok",
    );
  });

  it("throws PlynfProxyError on non-2xx", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(401, "bad key"));
    const client = new PlynfOpenAI({
      apiKey: "sk-bad",
      plynfUrl: "https://plynf.test",
      fetch: fetchMock as unknown as typeof fetch,
    });
    await expect(
      client.chat.completions.create({
        model: "gpt-4o",
        messages: [],
      }),
    ).rejects.toBeInstanceOf(PlynfProxyError);
  });

  it("streams SSE chunks and stops on [DONE]", async () => {
    const sse =
      'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n' +
      'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n' +
      "data: [DONE]\n";

    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(sse));
        controller.close();
      },
    });
    const fetchMock = vi.fn(async () =>
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );

    const client = new PlynfOpenAI({
      apiKey: "sk",
      plynfUrl: "https://plynf.test",
      fetch: fetchMock as unknown as typeof fetch,
    });
    const result = await client.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });
    const chunks: unknown[] = [];
    for await (const c of result as AsyncIterable<unknown>) {
      chunks.push(c);
    }
    expect(chunks).toHaveLength(2);
  });
});

describe("wrapTool", () => {
  it("calls /v1/shape and returns the shaped payload", async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      expect(String(url)).toMatch(/\/v1\/shape$/);
      const body = JSON.parse((init?.body as string) ?? "{}");
      expect(body.tool).toBe("getOrder");
      return jsonResponse(200, {
        shaped: { order_id: "12345", status: "in_transit" },
        shaped_by_plynf: true,
        raw_response_tokens: 200,
        shaped_response_tokens: 12,
        saved_tokens: 188,
        savings_pct: 0.94,
      });
    });

    const getOrder = async (orderId: string) => ({
      order_id: orderId,
      status: "in_transit",
      junk: "x".repeat(500),
    });
    const wrapped = wrapTool(getOrder as unknown as (...args: unknown[]) => Promise<unknown>, {
      plynfUrl: "https://plynf.test",
      apiKey: "pl-key",
      fetch: fetchMock as unknown as typeof fetch,
      toolName: "getOrder",
    });
    const result = await wrapped("12345");
    expect(result).toEqual({ order_id: "12345", status: "in_transit" });
    expect((wrapped as unknown as { __plynfWrapped: boolean }).__plynfWrapped).toBe(true);
    expect(wrapped.name).toBe("getOrder");
  });

  it("fails open when Plynf is unreachable", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(500, "down"));
    const raw = { Id: "1", Name: "Jane" };
    const wrapped = wrapTool(async () => raw, {
      plynfUrl: "https://plynf.test",
      apiKey: "pl-key",
      fetch: fetchMock as unknown as typeof fetch,
    });
    const result = await wrapped();
    expect(result).toEqual(raw);
  });

  it("wrapTools wraps a list and preserves names", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { shaped: null }));
    const a = async () => ({});
    Object.defineProperty(a, "name", { value: "a" });
    const b = async () => ({});
    Object.defineProperty(b, "name", { value: "b" });
    const wrapped = wrapTools([a, b], {
      plynfUrl: "https://plynf.test",
      apiKey: "pl-key",
      fetch: fetchMock as unknown as typeof fetch,
    });
    expect(wrapped.map((w) => w.name)).toEqual(["a", "b"]);
    expect(wrapped.every((w) => (w as unknown as { __plynfWrapped: boolean }).__plynfWrapped)).toBe(
      true,
    );
  });
});
