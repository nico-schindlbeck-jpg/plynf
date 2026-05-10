/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * OpenAI provider — wraps the official `openai` package.
 *
 * Models the chat-completions streaming endpoint; the Responses API is
 * intentionally out of scope. Pricing tracks the gpt-5 generation.
 *
 * The package is loaded via dynamic import so the base SDK works
 * without the optional peer dep installed.
 */

import { LLMProviderError, LLMProviderNotInstalledError, LLMRateLimitedError } from "../errors.js";
import type { LLMMessage, LLMResponse, LLMStreamChunk } from "../types.js";
import type { LLMProvider, LLMProviderRequest } from "./types.js";

/**
 * Per-token pricing in USD. Cost = tokens * rate.
 *
 * Source: openai.com/api/pricing (gpt-5 generation).
 */
export const OPENAI_PRICING: Record<string, { input: number; output: number }> = {
  "gpt-5": {
    input: 1.25 / 1_000_000,
    output: 10.0 / 1_000_000,
  },
  "gpt-5-mini": {
    input: 0.25 / 1_000_000,
    output: 2.0 / 1_000_000,
  },
  "gpt-5-nano": {
    input: 0.05 / 1_000_000,
    output: 0.4 / 1_000_000,
  },
};

/** Fallback pricing applied when the model isn't in {@link OPENAI_PRICING}. */
const FALLBACK_PRICING = OPENAI_PRICING["gpt-5-mini"]!;

/** Configuration accepted by `OpenAIProvider.create`. */
export interface OpenAIProviderConfig {
  /** API key. Defaults to `process.env.OPENAI_API_KEY`. */
  apiKey?: string;
  /** Override the API base URL (rare; mostly for proxies / Azure shims). */
  baseUrl?: string;
  /** Inject a pre-built client (mostly for tests using `vi.mock`). */
  client?: unknown;
}

/**
 * Plinth's adapter over `openai`'s chat-completions surface.
 *
 * Async constructor (`OpenAIProvider.create`) lazily imports the SDK
 * so users without the dep installed don't pay the cost.
 */
export class OpenAIProvider implements LLMProvider {
  readonly name = "openai";

  static async create(config: OpenAIProviderConfig = {}): Promise<OpenAIProvider> {
    if (config.client !== undefined) {
      return new OpenAIProvider(config.client);
    }
    let mod: { default?: unknown; OpenAI?: unknown };
    try {
      // Optional peer dep — the import path is dynamic so TS doesn't
      // try to resolve the module at compile time.
      const importPath = "openai";
      mod = (await import(importPath)) as { default?: unknown; OpenAI?: unknown };
    } catch (err) {
      throw new LLMProviderNotInstalledError(
        "The 'openai' provider requires the openai package. " +
          "Install it with: npm install openai",
        { cause: (err as Error).message },
      );
    }
    const Ctor = (mod.default ?? mod.OpenAI) as
      | (new (opts: Record<string, unknown>) => unknown)
      | undefined;
    if (typeof Ctor !== "function") {
      throw new LLMProviderNotInstalledError(
        "The installed openai package does not export the expected " +
          "constructor. Upgrade to >=4.40.0.",
      );
    }
    const apiKey = config.apiKey ?? process.env.OPENAI_API_KEY;
    const opts: Record<string, unknown> = {};
    if (apiKey) opts.apiKey = apiKey;
    if (config.baseUrl) opts.baseURL = config.baseUrl;
    const client = new Ctor(opts);
    return new OpenAIProvider(client);
  }

  constructor(private readonly client: unknown) {}

  // ------------------------------------------------------------------
  // Message translation
  // ------------------------------------------------------------------

  /** Normalise messages into the OpenAI chat schema. */
  private static toChat(messages: LLMMessage[]): Array<Record<string, unknown>> {
    return messages.map((msg) => {
      const out: Record<string, unknown> = { role: msg.role, content: msg.content };
      if (msg.name !== undefined) out.name = msg.name;
      if (msg.toolCallId !== undefined) out.tool_call_id = msg.toolCallId;
      return out;
    });
  }

  /** Return `[content, finishReason]` from a ChatCompletion. */
  private static extractContent(completion: unknown): [string, string] {
    const choices = (completion as { choices?: unknown }).choices;
    if (!Array.isArray(choices) || choices.length === 0) return ["", "stop"];
    const first = choices[0] as { message?: unknown; finish_reason?: unknown };
    const messageObj = first.message as { content?: unknown } | undefined;
    let content: string = "";
    if (messageObj && typeof messageObj.content === "string") content = messageObj.content;
    const finish = typeof first.finish_reason === "string" && first.finish_reason ? first.finish_reason : "stop";
    return [content, finish];
  }

  /** Return `[input, output]` token counts from a ChatCompletion. */
  private static extractUsage(completion: unknown): [number, number] {
    const usage = (completion as { usage?: unknown }).usage;
    if (!usage || typeof usage !== "object") return [0, 0];
    const u = usage as Record<string, unknown>;
    const inT = Number(u.prompt_tokens ?? u.input_tokens ?? 0);
    const outT = Number(u.completion_tokens ?? u.output_tokens ?? 0);
    return [
      Number.isFinite(inT) ? inT : 0,
      Number.isFinite(outT) ? outT : 0,
    ];
  }

  /** Best-effort conversion to a plain object for `raw`. */
  private static toDict(value: unknown): Record<string, unknown> {
    if (value && typeof value === "object") {
      try {
        return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
      } catch {
        return { raw: String(value) };
      }
    }
    return { raw: String(value) };
  }

  // ------------------------------------------------------------------
  // Cost
  // ------------------------------------------------------------------

  estimateCostUsd(model: string, inputTokens: number, outputTokens: number): number {
    const pricing = OPENAI_PRICING[model] ?? FALLBACK_PRICING;
    return inputTokens * pricing.input + outputTokens * pricing.output;
  }

  // ------------------------------------------------------------------
  // Error mapping
  // ------------------------------------------------------------------

  private wrapError(exc: unknown): Error {
    const e = exc as {
      status?: number;
      message?: string;
      response?: { headers?: Record<string, string>; status?: number };
      body?: unknown;
      headers?: Record<string, string>;
      name?: string;
    };
    const message = typeof e.message === "string" ? e.message : String(exc);
    const status = e.status ?? e.response?.status;
    if (status === 429 || (typeof e.name === "string" && /rate.?limit/i.test(e.name))) {
      const retryAfter = parseRetryAfter(e);
      return new LLMRateLimitedError(message, {
        retryAfterSeconds: retryAfter,
        statusCode: status ?? 429,
        body: e.body,
        provider: this.name,
      });
    }
    if (typeof status === "number") {
      return new LLMProviderError(message, {
        statusCode: status,
        body: e.body,
        provider: this.name,
      });
    }
    return new LLMProviderError(message, { provider: this.name });
  }

  // ------------------------------------------------------------------
  // Surface
  // ------------------------------------------------------------------

  async complete(req: LLMProviderRequest): Promise<LLMResponse> {
    const apiArgs: Record<string, unknown> = {
      model: req.model,
      messages: OpenAIProvider.toChat(req.messages),
    };
    if (req.maxTokens !== undefined) apiArgs.max_tokens = req.maxTokens;
    if (req.temperature !== undefined) apiArgs.temperature = req.temperature;
    if (req.topP !== undefined) apiArgs.top_p = req.topP;
    if (req.stopSequences !== undefined && req.stopSequences.length > 0) {
      apiArgs.stop = req.stopSequences;
    }
    if (req.extra) Object.assign(apiArgs, req.extra);

    let completion: unknown;
    try {
      const create = ((this.client as { chat?: unknown }).chat as { completions?: unknown } | undefined)
        ?.completions as { create?: (args: unknown) => Promise<unknown> } | undefined;
      if (!create || typeof create.create !== "function") {
        throw new Error("OpenAI client is missing `chat.completions.create`");
      }
      completion = await create.create(apiArgs);
    } catch (err) {
      throw this.wrapError(err);
    }

    const [content, finishReason] = OpenAIProvider.extractContent(completion);
    const [inT, outT] = OpenAIProvider.extractUsage(completion);
    const reportedModel = (completion as { model?: unknown }).model;
    return {
      content,
      model: typeof reportedModel === "string" ? reportedModel : req.model,
      finishReason,
      inputTokens: inT,
      outputTokens: outT,
      costUsd: this.estimateCostUsd(req.model, inT, outT),
      durationMs: 0,
      provider: this.name,
      raw: OpenAIProvider.toDict(completion),
    };
  }

  async *stream(req: LLMProviderRequest): AsyncGenerator<LLMStreamChunk, void, void> {
    const apiArgs: Record<string, unknown> = {
      model: req.model,
      messages: OpenAIProvider.toChat(req.messages),
      stream: true,
    };
    if (req.maxTokens !== undefined) apiArgs.max_tokens = req.maxTokens;
    if (req.temperature !== undefined) apiArgs.temperature = req.temperature;
    if (req.topP !== undefined) apiArgs.top_p = req.topP;
    if (req.stopSequences !== undefined && req.stopSequences.length > 0) {
      apiArgs.stop = req.stopSequences;
    }
    if (req.extra) Object.assign(apiArgs, req.extra);

    let stream: AsyncIterable<unknown>;
    try {
      const create = ((this.client as { chat?: unknown }).chat as { completions?: unknown } | undefined)
        ?.completions as { create?: (args: unknown) => Promise<AsyncIterable<unknown>> } | undefined;
      if (!create || typeof create.create !== "function") {
        throw new Error("OpenAI client is missing `chat.completions.create`");
      }
      stream = await create.create(apiArgs);
    } catch (err) {
      throw this.wrapError(err);
    }

    let finishReason: string | undefined;
    try {
      for await (const chunk of stream) {
        if (!chunk || typeof chunk !== "object") continue;
        const choices = (chunk as { choices?: unknown }).choices;
        if (!Array.isArray(choices) || choices.length === 0) continue;
        const first = choices[0] as { delta?: unknown; finish_reason?: unknown };
        const delta = first.delta as { content?: unknown } | undefined;
        if (delta && typeof delta.content === "string" && delta.content.length > 0) {
          yield { delta: delta.content, raw: {} };
        }
        if (typeof first.finish_reason === "string" && first.finish_reason) {
          finishReason = first.finish_reason;
        }
      }
    } catch (err) {
      throw this.wrapError(err);
    }

    yield { delta: "", finishReason: finishReason ?? "stop", raw: {} };
  }
}

function parseRetryAfter(err: {
  headers?: Record<string, string>;
  response?: { headers?: Record<string, string> };
}): number | null {
  const headers = err.headers ?? err.response?.headers;
  if (!headers) return null;
  const raw = headers["retry-after"] ?? headers["Retry-After"] ?? null;
  if (raw === null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}
