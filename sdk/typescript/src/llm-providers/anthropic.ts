/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Anthropic provider — wraps the official `@anthropic-ai/sdk` package.
 *
 * The provider lives behind the optional `@anthropic-ai/sdk` peer
 * dependency: `npm install @anthropic-ai/sdk`. If the package is
 * missing, {@link AnthropicProvider.create} throws
 * {@link LLMProviderNotInstalledError} with the install hint.
 *
 * The cost helper uses the published per-million-token pricing for the
 * v4.5 generation. Update {@link ANTHROPIC_PRICING} when Anthropic's
 * rates change — the table is the single source of truth for cost
 * calculation.
 */

import { LLMProviderError, LLMProviderNotInstalledError, LLMRateLimitedError } from "../errors.js";
import type { LLMMessage, LLMResponse, LLMStreamChunk } from "../types.js";
import type { LLMProvider, LLMProviderRequest } from "./types.js";

/**
 * Per-token pricing in USD. Cost = tokens * rate.
 *
 * Source: anthropic.com/pricing (v4.5 series).
 */
export const ANTHROPIC_PRICING: Record<string, { input: number; output: number }> = {
  "claude-sonnet-4-5": {
    input: 3.0 / 1_000_000,
    output: 15.0 / 1_000_000,
  },
  "claude-opus-4-5": {
    input: 15.0 / 1_000_000,
    output: 75.0 / 1_000_000,
  },
  "claude-haiku-4-5": {
    input: 0.8 / 1_000_000,
    output: 4.0 / 1_000_000,
  },
};

/**
 * Fallback pricing applied when the model name is unknown. We pick the
 * Sonnet rate because it's the most common production target — keeps
 * budgeting roughly correct on new aliases the table hasn't seen yet.
 */
const FALLBACK_PRICING = ANTHROPIC_PRICING["claude-sonnet-4-5"]!;

/** Configuration accepted by `AnthropicProvider.create`. */
export interface AnthropicProviderConfig {
  /** API key. Defaults to `process.env.ANTHROPIC_API_KEY`. */
  apiKey?: string;
  /** Override the API base URL (rare; mostly for proxies). */
  baseUrl?: string;
  /** Inject a pre-built client (mostly for tests using `vi.mock`). */
  client?: unknown;
}

/** Result of `_splitSystem` — Anthropic puts the system prompt outside `messages`. */
interface SplitMessages {
  system: string | null;
  messages: Array<{ role: "user" | "assistant"; content: string }>;
}

/**
 * Plinth's adapter over `@anthropic-ai/sdk`.
 *
 * Construction is async because `@anthropic-ai/sdk` is loaded via
 * dynamic import — this keeps the SDK tree-shakeable and lets users
 * who never call `useProvider("anthropic")` skip the install.
 */
export class AnthropicProvider implements LLMProvider {
  readonly name = "anthropic";

  /**
   * Build the provider, lazily importing `@anthropic-ai/sdk`.
   *
   * Throws {@link LLMProviderNotInstalledError} if the package isn't
   * installed; throws {@link LLMProviderError} with no `statusCode` if
   * the constructor itself rejects (e.g. malformed API key).
   */
  static async create(config: AnthropicProviderConfig = {}): Promise<AnthropicProvider> {
    if (config.client !== undefined) {
      // Test path — caller supplies a fake `Anthropic` client object.
      return new AnthropicProvider(config.client);
    }
    let mod: { default?: unknown; Anthropic?: unknown };
    try {
      // The vendor SDK is an optional peer dep; the import below is
      // only valid when the user has installed it. We use a runtime
      // string to suppress TS's static module resolution, then cast.
      const importPath = "@anthropic-ai/sdk";
      mod = (await import(importPath)) as {
        default?: unknown;
        Anthropic?: unknown;
      };
    } catch (err) {
      throw new LLMProviderNotInstalledError(
        "The 'anthropic' provider requires @anthropic-ai/sdk. " +
          "Install it with: npm install @anthropic-ai/sdk",
        { cause: (err as Error).message },
      );
    }
    // The SDK's surface has shifted between major versions — the
    // constructor lives at `default` (v0.28+) or `Anthropic` (older).
    const Ctor = (mod.default ?? mod.Anthropic) as
      | (new (opts: Record<string, unknown>) => unknown)
      | undefined;
    if (typeof Ctor !== "function") {
      throw new LLMProviderNotInstalledError(
        "The installed @anthropic-ai/sdk does not export the expected " +
          "constructor. Upgrade to >=0.30.0.",
      );
    }
    const apiKey = config.apiKey ?? process.env.ANTHROPIC_API_KEY;
    const opts: Record<string, unknown> = {};
    if (apiKey) opts.apiKey = apiKey;
    if (config.baseUrl) opts.baseURL = config.baseUrl;
    const client = new Ctor(opts);
    return new AnthropicProvider(client);
  }

  /**
   * Direct constructor — caller supplies a configured client.
   *
   * `LLMClient.useProvider("anthropic", ...)` calls
   * {@link AnthropicProvider.create} and awaits the result; tests can
   * `new AnthropicProvider(fakeClient)` directly.
   */
  constructor(private readonly client: unknown) {}

  // ------------------------------------------------------------------
  // Message translation
  // ------------------------------------------------------------------

  /**
   * Split system messages out and reformat the rest.
   *
   * Anthropic's API takes the system prompt as a top-level argument;
   * we concatenate multiple system messages with a blank line so callers
   * can compose them without rebuilding the prompt themselves.
   */
  private static splitSystem(messages: LLMMessage[]): SplitMessages {
    const systemParts: string[] = [];
    const chat: Array<{ role: "user" | "assistant"; content: string }> = [];
    for (const msg of messages) {
      if (msg.role === "system") {
        if (msg.content) systemParts.push(msg.content);
        continue;
      }
      // Anthropic's role enum is `user` | `assistant`. Tool messages
      // aren't supported on the simple-chat surface; collapse anything
      // unknown to `user` so the call doesn't 400.
      const apiRole: "user" | "assistant" = msg.role === "assistant" ? "assistant" : "user";
      chat.push({ role: apiRole, content: msg.content });
    }
    return {
      system: systemParts.length > 0 ? systemParts.join("\n\n") : null,
      messages: chat,
    };
  }

  /** Pull the assistant's text out of an Anthropic `Message`. */
  private static extractContent(message: unknown): string {
    const blocks = (message as { content?: unknown }).content;
    if (!Array.isArray(blocks)) return "";
    const parts: string[] = [];
    for (const block of blocks) {
      if (block && typeof block === "object" && typeof (block as { text?: unknown }).text === "string") {
        parts.push((block as { text: string }).text);
      }
    }
    return parts.join("");
  }

  /** Return `[input, output]` token counts from a response, defaulting to 0. */
  private static extractUsage(message: unknown): [number, number] {
    const usage = (message as { usage?: unknown }).usage;
    if (!usage || typeof usage !== "object") return [0, 0];
    const u = usage as Record<string, unknown>;
    const inT = Number(u.input_tokens ?? u.prompt_tokens ?? 0);
    const outT = Number(u.output_tokens ?? u.completion_tokens ?? 0);
    return [
      Number.isFinite(inT) ? inT : 0,
      Number.isFinite(outT) ? outT : 0,
    ];
  }

  /** Best-effort conversion to a plain object for `raw`. */
  private static toDict(message: unknown): Record<string, unknown> {
    if (message && typeof message === "object") {
      try {
        return JSON.parse(JSON.stringify(message)) as Record<string, unknown>;
      } catch {
        return { raw: String(message) };
      }
    }
    return { raw: String(message) };
  }

  // ------------------------------------------------------------------
  // Cost
  // ------------------------------------------------------------------

  estimateCostUsd(model: string, inputTokens: number, outputTokens: number): number {
    const pricing = ANTHROPIC_PRICING[model] ?? FALLBACK_PRICING;
    return inputTokens * pricing.input + outputTokens * pricing.output;
  }

  // ------------------------------------------------------------------
  // Error mapping
  // ------------------------------------------------------------------

  /** Translate a vendor SDK error to a Plinth `LLMProviderError`. */
  private wrapError(exc: unknown): Error {
    // The SDK's typed errors carry `status` and `headers`. We don't
    // `instanceof` the classes — that would require a static import
    // and defeat the lazy-import design. Instead we duck-type on the
    // shape: an `APIStatusError` has `status` (number) and may have
    // `headers` (object). Rate-limit specifically also has status 429.
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
    // Connection / unknown error.
    return new LLMProviderError(message, { provider: this.name });
  }

  // ------------------------------------------------------------------
  // Surface
  // ------------------------------------------------------------------

  async complete(req: LLMProviderRequest): Promise<LLMResponse> {
    const split = AnthropicProvider.splitSystem(req.messages);
    const apiArgs: Record<string, unknown> = {
      model: req.model,
      messages: split.messages,
      // Anthropic requires `max_tokens` — default 1024 matches Python.
      max_tokens: req.maxTokens ?? 1024,
    };
    if (split.system !== null) apiArgs.system = split.system;
    if (req.temperature !== undefined) apiArgs.temperature = req.temperature;
    if (req.topP !== undefined) apiArgs.top_p = req.topP;
    if (req.stopSequences !== undefined && req.stopSequences.length > 0) {
      apiArgs.stop_sequences = req.stopSequences;
    }
    if (req.extra) Object.assign(apiArgs, req.extra);

    let message: unknown;
    try {
      const messages = (this.client as { messages?: unknown }).messages as
        | { create?: (args: unknown) => Promise<unknown> }
        | undefined;
      if (!messages || typeof messages.create !== "function") {
        throw new Error("Anthropic client is missing `messages.create`");
      }
      message = await messages.create(apiArgs);
    } catch (err) {
      throw this.wrapError(err);
    }

    const text = AnthropicProvider.extractContent(message);
    const [inT, outT] = AnthropicProvider.extractUsage(message);
    const stopReason = (message as { stop_reason?: unknown }).stop_reason;
    const finishReason = typeof stopReason === "string" && stopReason ? stopReason : "stop";
    const reportedModel = (message as { model?: unknown }).model;
    return {
      content: text,
      model: typeof reportedModel === "string" ? reportedModel : req.model,
      finishReason,
      inputTokens: inT,
      outputTokens: outT,
      costUsd: this.estimateCostUsd(req.model, inT, outT),
      durationMs: 0, // populated by `LLMClient` using its own clock
      provider: this.name,
      raw: AnthropicProvider.toDict(message),
    };
  }

  async *stream(req: LLMProviderRequest): AsyncGenerator<LLMStreamChunk, void, void> {
    const split = AnthropicProvider.splitSystem(req.messages);
    const apiArgs: Record<string, unknown> = {
      model: req.model,
      messages: split.messages,
      max_tokens: req.maxTokens ?? 1024,
      stream: true,
    };
    if (split.system !== null) apiArgs.system = split.system;
    if (req.temperature !== undefined) apiArgs.temperature = req.temperature;
    if (req.topP !== undefined) apiArgs.top_p = req.topP;
    if (req.stopSequences !== undefined && req.stopSequences.length > 0) {
      apiArgs.stop_sequences = req.stopSequences;
    }
    if (req.extra) Object.assign(apiArgs, req.extra);

    let stream: AsyncIterable<unknown>;
    try {
      const messages = (this.client as { messages?: unknown }).messages as
        | {
            create?: (args: unknown) => Promise<AsyncIterable<unknown>>;
            stream?: (args: unknown) => AsyncIterable<unknown>;
          }
        | undefined;
      if (!messages) throw new Error("Anthropic client is missing `messages`");
      // Prefer `messages.stream` (returns AsyncIterable directly) when
      // present; fall back to `messages.create` with `stream: true`.
      if (typeof messages.stream === "function") {
        stream = messages.stream(apiArgs);
      } else if (typeof messages.create === "function") {
        stream = await messages.create(apiArgs);
      } else {
        throw new Error("Anthropic client is missing both `messages.stream` and `messages.create`");
      }
    } catch (err) {
      throw this.wrapError(err);
    }

    let finishReason: string | undefined;
    let lastRaw: Record<string, unknown> = {};
    try {
      for await (const event of stream) {
        // The SDK emits typed events; we care about
        //   `content_block_delta` (text deltas)
        //   `message_delta` (carries stop_reason)
        //   `message_stop` (terminal marker)
        if (event && typeof event === "object") {
          const e = event as Record<string, unknown>;
          const type = typeof e.type === "string" ? e.type : "";
          if (type === "content_block_delta") {
            const delta = e.delta as { type?: unknown; text?: unknown } | undefined;
            if (delta && typeof delta.text === "string" && delta.text.length > 0) {
              yield { delta: delta.text, raw: {} };
            }
          } else if (type === "message_delta") {
            const inner = (e.delta as { stop_reason?: unknown }) ?? {};
            if (typeof inner.stop_reason === "string") finishReason = inner.stop_reason;
          } else if (typeof (e as { delta?: unknown }).delta === "string") {
            // Some test doubles emit `{ delta: "abc" }` directly.
            yield { delta: (e as { delta: string }).delta, raw: {} };
          }
          lastRaw = e as Record<string, unknown>;
        }
      }
    } catch (err) {
      throw this.wrapError(err);
    }

    yield { delta: "", finishReason: finishReason ?? "stop", raw: lastRaw };
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract a `retry-after` hint from an SDK error.
 *
 * Anthropic surfaces the header on `error.headers` (newer SDKs) or
 * `error.response.headers`. A best-effort lookup keeps the SDK working
 * when the upstream layout shifts between minor versions.
 */
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
