/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * The Plinth LLM facade.
 *
 * The {@link LLMClient} wraps a pluggable {@link LLMProvider}, adds a
 * retry loop with exponential back-off + `Retry-After` honouring, and
 * records each call into the gateway audit log so cost shows up on the
 * existing dashboard pipeline.
 *
 * Audit attribution: each successful LLM call posts to
 * `POST /v1/audit/record-llm` on the gateway with a synthetic
 * `tool_id="llm.<provider>"`. Existing dashboards keying on tool_id
 * prefixes pick this up automatically. Failures of the audit POST are
 * swallowed — an LLM call must never fail because the audit endpoint
 * is unreachable.
 *
 * Mirrors `plinth.llm.LLMClient` in the Python SDK (v1.2.1).
 */

import {
  LLMProviderError,
  LLMProviderNotConfiguredError,
  LLMRateLimitedError,
  LLMRetryExhaustedError,
} from "./errors.js";
import { buildProvider } from "./llm-providers/index.js";
import type { LLMProvider, LLMProviderRequest } from "./llm-providers/types.js";
import { count as countTokens } from "./tokens.js";
import type {
  LLMMessage,
  LLMProviderConfig,
  LLMProviderName,
  LLMRequest,
  LLMResponse,
  LLMStreamChunk,
} from "./types.js";

/**
 * Internal hook surface that {@link LLMClient} reads off the owning
 * `Plinth` facade. Kept narrow so this module doesn't depend on the
 * full client class — avoids a cycle through `client.ts`.
 */
export interface LLMClientHost {
  readonly gatewayUrl: string;
  readonly apiKey: string;
  /** Optional fetch override (tests). */
  readonly fetch: typeof fetch;
}

/** Tunables exposed to {@link LLMClient.configureRetries}. */
export interface RetryConfig {
  /** Maximum retry attempts on retryable errors. `0` disables retries. */
  retries?: number;
  /**
   * Base for exponential back-off — actual wait is `base * 2**attempt`.
   * Capped at 30s. Set to `0` to skip the wait entirely (tests).
   */
  backoffSeconds?: number;
}

/** Maximum back-off time in seconds, matching the Python SDK. */
const BACKOFF_CAP_SECONDS = 30;

/**
 * The `client.llm` namespace.
 *
 * Owns one {@link LLMProvider} and exposes the user-facing
 * `complete` / `stream` methods on top of it. Adds:
 *
 *   * Retry-with-back-off on 429 (`Retry-After`-aware) and 5xx.
 *   * Audit-event recording on success via the gateway's
 *     `/v1/audit/record-llm` endpoint.
 *   * Auto-detection: if no provider is configured but
 *     `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) is set, the first
 *     call lazily configures the matching built-in.
 *
 * @example
 * ```ts
 * client.llm.useProvider("mock", { responses: ["hi"] });
 * const r = await client.llm.complete({
 *   model: "claude-sonnet-4-5",
 *   messages: [{ role: "user", content: "hello" }],
 * });
 * console.log(r.content, r.costUsd);
 * ```
 */
export class LLMClient {
  private provider: LLMProvider | null = null;
  private retries = 3;
  private retryBackoffSeconds = 1.0;
  /**
   * Test seam — when set, replaces `setTimeout`-based sleeps. Lets the
   * test suite drive the retry loop synchronously without hanging on
   * back-off waits.
   */
  private sleepImpl: (ms: number) => Promise<void> = defaultSleep;

  constructor(private readonly host: LLMClientHost) {}

  // ------------------------------------------------------------------
  // Provider configuration
  // ------------------------------------------------------------------

  /** Return the active provider (or `null` if not configured). */
  getProvider(): LLMProvider | null {
    return this.provider;
  }

  /**
   * Configure one of the built-in providers by name.
   *
   * Returns the configured provider so callers can chain or hold a
   * reference for inspection. Async because Anthropic / OpenAI providers
   * dynamic-import their vendor SDKs.
   */
  async useProvider(
    name: LLMProviderName | string,
    config: LLMProviderConfig | Record<string, unknown> = {},
  ): Promise<LLMProvider> {
    const provider = await buildProvider(name, config as Record<string, unknown>);
    this.provider = provider;
    return provider;
  }

  /**
   * Plug in a custom provider implementing {@link LLMProvider}.
   *
   * Useful for in-house gateways, non-built-in vendors, or thin
   * recording wrappers in tests.
   */
  useCustomProvider(provider: LLMProvider): void {
    this.provider = provider;
  }

  /** Tune the retry loop without rebuilding the client. */
  configureRetries(config: RetryConfig): void {
    if (config.retries !== undefined) {
      this.retries = Math.max(0, Math.floor(config.retries));
    }
    if (config.backoffSeconds !== undefined) {
      this.retryBackoffSeconds = Math.max(0, config.backoffSeconds);
    }
  }

  /**
   * Test-only: replace the sleep function used between retries.
   *
   * @internal
   */
  _setSleepForTests(sleep: (ms: number) => Promise<void>): void {
    this.sleepImpl = sleep;
  }

  // ------------------------------------------------------------------
  // Auto-detection
  // ------------------------------------------------------------------

  /**
   * Resolve the active provider, attempting auto-detection first.
   *
   * If no provider has been explicitly configured but the environment
   * exposes `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, lazily build the
   * matching built-in. Anthropic wins on a tie (matches the Python SDK
   * — Claude-first house style).
   */
  private async ensureProvider(): Promise<LLMProvider> {
    if (this.provider !== null) return this.provider;
    if (process.env.ANTHROPIC_API_KEY) {
      return await this.useProvider("anthropic");
    }
    if (process.env.OPENAI_API_KEY) {
      return await this.useProvider("openai");
    }
    throw new LLMProviderNotConfiguredError(
      "No LLM provider configured. Call client.llm.useProvider(" +
        "'anthropic'|'openai'|'mock') first, or set " +
        "ANTHROPIC_API_KEY / OPENAI_API_KEY in the environment.",
    );
  }

  // ------------------------------------------------------------------
  // Retry loop
  // ------------------------------------------------------------------

  /** True for 5xx errors. 4xx (other than 429) are not retried. */
  private static isRetryableStatus(status: number | undefined): boolean {
    return typeof status === "number" && status >= 500 && status < 600;
  }

  /** Compute back-off in milliseconds for a given attempt index (0-based). */
  private backoffMs(attempt: number): number {
    const seconds = Math.min(this.retryBackoffSeconds * 2 ** attempt, BACKOFF_CAP_SECONDS);
    return Math.max(0, seconds * 1000);
  }

  /** Run `fn` with retry on 429 / 5xx; rethrow as `LLMRetryExhaustedError`. */
  private async retryLoop<T>(fn: () => Promise<T>): Promise<T> {
    let lastError: Error | undefined;
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      try {
        return await fn();
      } catch (err) {
        lastError = err as Error;
        if (err instanceof LLMRateLimitedError) {
          if (attempt >= this.retries) break;
          const hint = err.retryAfterSeconds;
          const waitMs =
            hint !== null && hint !== undefined && hint > 0
              ? hint * 1000
              : this.backoffMs(attempt);
          await this.sleepImpl(waitMs);
          continue;
        }
        if (err instanceof LLMProviderError) {
          if (
            !LLMClient.isRetryableStatus(err.statusCode) ||
            attempt >= this.retries
          ) {
            throw err;
          }
          await this.sleepImpl(this.backoffMs(attempt));
          continue;
        }
        // Non-LLM error — bubble immediately (e.g. a Plinth-side bug).
        throw err;
      }
    }
    throw new LLMRetryExhaustedError(
      `LLM call failed after ${this.retries + 1} attempts: ${lastError?.message ?? "unknown"}`,
      this.retries + 1,
      lastError,
    );
  }

  // ------------------------------------------------------------------
  // Audit recording
  // ------------------------------------------------------------------

  /** Build the body for `POST /v1/audit/record-llm`. */
  private buildAuditPayload(
    response: LLMResponse,
    request: { workspaceId?: string; agentId?: string },
  ): Record<string, unknown> {
    // Server expects snake_case (`extra="forbid"` on the Pydantic model).
    return {
      tool_id: `llm.${response.provider}`,
      model: response.model,
      input_tokens: Math.max(0, Math.trunc(response.inputTokens)),
      output_tokens: Math.max(0, Math.trunc(response.outputTokens)),
      cost_usd: Math.max(0, Number(response.costUsd) || 0),
      duration_ms: Math.max(0, Math.trunc(response.durationMs)),
      workspace_id: request.workspaceId ?? null,
      agent_id: request.agentId ?? null,
      finish_reason: response.finishReason,
    };
  }

  /**
   * Best-effort audit POST. Mutates `response.auditId` on success.
   *
   * Failures are swallowed — the LLM call has already happened and the
   * user's program should not crash because of an observability blip.
   */
  private async recordAudit(
    response: LLMResponse,
    request: { workspaceId?: string; agentId?: string },
  ): Promise<void> {
    if (!this.host.gatewayUrl) return;
    try {
      const url = `${this.host.gatewayUrl.replace(/\/+$/, "")}/v1/audit/record-llm`;
      const res = await this.host.fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.host.apiKey}`,
        },
        body: JSON.stringify(this.buildAuditPayload(response, request)),
      });
      if (!res.ok) return;
      try {
        const data = (await res.json()) as { audit_id?: unknown };
        if (typeof data.audit_id === "string") {
          response.auditId = data.audit_id;
        }
      } catch {
        // Ignore malformed JSON — never fail an LLM call.
      }
    } catch {
      // Network / abort / dns — swallow.
    }
  }

  // ------------------------------------------------------------------
  // Public surface
  // ------------------------------------------------------------------

  /** Run a non-streaming LLM completion. */
  async complete(req: LLMRequest): Promise<LLMResponse> {
    const provider = await this.ensureProvider();
    const start = Date.now();
    const providerReq = LLMClient.toProviderRequest(req);
    const response = await this.retryLoop(() => provider.complete(providerReq));
    // Always re-time at the facade so durationMs is consistent across
    // providers (some adapters return 0 because they don't measure
    // inside the wrapper).
    response.durationMs = Date.now() - start;
    await this.recordAudit(response, req);
    return response;
  }

  /**
   * Stream chunks; record audit after the iterator drains.
   *
   * Streaming bypasses the retry loop because by the time a transient
   * failure is visible, chunks are already flowing to the caller. Retry
   * policy for streaming is documented as "no automatic retries —
   * caller restarts".
   */
  async *stream(req: LLMRequest): AsyncGenerator<LLMStreamChunk, void, void> {
    const provider = await this.ensureProvider();
    const start = Date.now();
    const providerReq = LLMClient.toProviderRequest(req);

    const accumulated: string[] = [];
    let finishReason: string | undefined;
    let lastRaw: Record<string, unknown> = {};

    for await (const chunk of provider.stream(providerReq)) {
      if (chunk.delta) accumulated.push(chunk.delta);
      if (chunk.finishReason !== undefined) finishReason = chunk.finishReason;
      if (chunk.raw && Object.keys(chunk.raw).length > 0) lastRaw = chunk.raw;
      yield chunk;
    }

    const durationMs = Date.now() - start;
    const text = accumulated.join("");
    // Streaming usually omits per-message usage; fall back to local
    // token counting via gpt-tokenizer so cost still flows through audit.
    const inputTokens = await approxInputTokens(req.messages);
    const outputTokens = await countTokens(text);
    const costUsd = provider.estimateCostUsd(req.model, inputTokens, outputTokens);
    const synthesized: LLMResponse = {
      content: text,
      model: req.model,
      finishReason: finishReason ?? "stop",
      inputTokens,
      outputTokens,
      costUsd,
      durationMs,
      provider: provider.name,
      raw: lastRaw,
    };
    await this.recordAudit(synthesized, req);
  }

  /** Translate a public {@link LLMRequest} into the provider-shaped form. */
  private static toProviderRequest(req: LLMRequest): LLMProviderRequest {
    const out: LLMProviderRequest = {
      model: req.model,
      messages: req.messages,
    };
    if (req.maxTokens !== undefined) out.maxTokens = req.maxTokens;
    if (req.temperature !== undefined) out.temperature = req.temperature;
    if (req.topP !== undefined) out.topP = req.topP;
    if (req.stopSequences !== undefined) out.stopSequences = req.stopSequences;
    if (req.extra !== undefined) out.extra = req.extra;
    return out;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Default sleep — used outside tests; cleared by `_setSleepForTests`. */
function defaultSleep(ms: number): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Rough offline input-token count via the SDK's `tokens` module. */
async function approxInputTokens(messages: LLMMessage[]): Promise<number> {
  return await countTokens(messages.map((m) => m.content).join("\n"));
}
