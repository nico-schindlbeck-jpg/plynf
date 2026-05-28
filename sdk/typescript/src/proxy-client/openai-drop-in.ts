/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * OpenAI-compatible client that routes through the Plynf proxy.
 *
 * Mirrors the surface area of the official `openai` Node SDK for the
 * pieces we actually need: `client.chat.completions.create(...)` with
 * sync and streaming modes.
 *
 * If you already use `openai` in your app, you can keep using it —
 * just set `baseURL` to your Plynf URL and Plynf still applies its
 * shaping. This SDK is for environments where pulling the full openai
 * package isn't desirable (Cloudflare Workers, edge runtimes, etc.).
 */

import type {
  ChatCompletionChunk,
  ChatCompletionRequest,
  ChatCompletionResponse,
} from "./types.js";

export class PlynfProxyError extends Error {
  constructor(public status: number, public body: string) {
    super(`Plynf proxy returned ${status}: ${body.slice(0, 300)}`);
    this.name = "PlynfProxyError";
  }
}

export interface PlynfOpenAIOptions {
  apiKey: string;
  plynfUrl: string;
  timeoutMs?: number;
  defaultHeaders?: Record<string, string>;
  fetch?: typeof fetch;
}

export class PlynfOpenAI {
  private readonly baseUrl: string;
  private readonly authHeader: string;
  private readonly timeoutMs: number;
  private readonly extraHeaders: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: PlynfOpenAIOptions) {
    this.baseUrl = opts.plynfUrl.replace(/\/+$/, "");
    this.authHeader = `Bearer ${opts.apiKey}`;
    this.timeoutMs = opts.timeoutMs ?? 60_000;
    this.extraHeaders = { ...(opts.defaultHeaders ?? {}) };
    this.fetchImpl = opts.fetch ?? fetch;
  }

  readonly chat = {
    completions: {
      create: this.createCompletion.bind(this),
    },
  };

  private headers(): Record<string, string> {
    return {
      "Content-Type": "application/json",
      Authorization: this.authHeader,
      ...this.extraHeaders,
    };
  }

  async createCompletion(
    body: ChatCompletionRequest,
  ): Promise<ChatCompletionResponse | AsyncIterable<ChatCompletionChunk>> {
    const stream = Boolean(body.stream);
    const url = `${this.baseUrl}/v1/chat/completions`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);

    const resp = await this.fetchImpl(url, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ ...body, stream }),
      signal: controller.signal,
    }).finally(() => clearTimeout(timer));

    if (!resp.ok) {
      const text = await resp.text();
      throw new PlynfProxyError(resp.status, text);
    }

    if (!stream) {
      return (await resp.json()) as ChatCompletionResponse;
    }
    return this.iterStream(resp);
  }

  private async *iterStream(resp: Response): AsyncIterable<ChatCompletionChunk> {
    if (!resp.body) return;
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice("data:".length).trim();
        if (payload === "[DONE]") return;
        try {
          yield JSON.parse(payload) as ChatCompletionChunk;
        } catch {
          // Ignore malformed chunks rather than aborting the stream.
        }
      }
    }
  }
}
