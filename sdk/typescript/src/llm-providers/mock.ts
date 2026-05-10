/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Deterministic LLM provider for tests + offline demos.
 *
 * Mirrors the Python SDK's `plinth.llm_providers.mock.MockProvider`:
 * cycles through a list of canned responses and shapes them into
 * realistic {@link LLMResponse} objects (token counts via
 * `gpt-tokenizer`, cost computed from the hardcoded pricing in
 * {@link MOCK_PRICING}).
 *
 * The provider runs with **zero network I/O**, so it's the default
 * choice in the SDK test suite (no API keys required).
 */

import { count as countTokens } from "../tokens.js";
import type { LLMMessage, LLMResponse, LLMStreamChunk } from "../types.js";
import type { LLMProvider, LLMProviderRequest } from "./types.js";

/**
 * Per-token pricing used for cost estimates.
 *
 * Numbers are USD/token (not USD/Mtok). Identical for input and output
 * so the math is obvious in tests; the real providers override this
 * with vendor pricing.
 */
export const MOCK_PRICING: Record<string, { input: number; output: number }> = {
  "mock-default": {
    input: 1.0 / 1_000_000,
    output: 2.0 / 1_000_000,
  },
};

/** Configuration accepted by `new MockProvider({...})`. */
export interface MockProviderConfig {
  /**
   * Ordered list of canned responses. Each item is either a `string` used
   * directly or an object with `{ content?: string }` — anything else is
   * coerced via `String(item)`. Cycles around so callers don't need to
   * count exact invocations.
   */
  responses?: Array<string | { content?: string; [key: string]: unknown }>;
  /** Model name reported in `LLMResponse`. Defaults to `"mock-model"`. */
  defaultModel?: string;
  /** Finish reason on responses. Defaults to `"stop"`. */
  finishReason?: string;
  /**
   * Approximate characters per streaming chunk. Smaller values produce
   * more chunks; the provider always emits at least one terminal chunk.
   */
  chunkSize?: number;
}

/**
 * Cycles through a list of canned responses.
 *
 * Used by the SDK test suite as the default, network-free provider. Also
 * useful for application-side dry-run modes.
 */
export class MockProvider implements LLMProvider {
  readonly name = "mock";

  private readonly responses: string[];
  private readonly defaultModel: string;
  private readonly finishReason: string;
  private readonly chunkSize: number;
  private cursor = 0;

  constructor(config: MockProviderConfig = {}) {
    const raw = config.responses ?? ["mock response"];
    this.responses = raw.map((r) => MockProvider.normalize(r));
    this.defaultModel = config.defaultModel ?? "mock-model";
    this.finishReason = config.finishReason ?? "stop";
    // chunkSize is clamped to >=1 so we never produce a zero-stride loop.
    this.chunkSize = Math.max(1, Math.floor(config.chunkSize ?? 16));
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  /** Coerce a canned response item to a plain string. */
  private static normalize(item: string | { content?: string; [k: string]: unknown }): string {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      const content = (item as { content?: unknown }).content;
      if (typeof content === "string") return content;
      try {
        return JSON.stringify(item);
      } catch {
        return String(item);
      }
    }
    return String(item);
  }

  /** Return the next canned response, cycling on exhaustion. */
  private next(): string {
    const text = this.responses[this.cursor % this.responses.length] ?? "";
    this.cursor++;
    return text;
  }

  /** Concatenate message contents for token-counting. */
  private static inputText(messages: LLMMessage[]): string {
    return messages.map((m) => m.content).join("\n");
  }

  // ------------------------------------------------------------------
  // Cost
  // ------------------------------------------------------------------

  estimateCostUsd(model: string, inputTokens: number, outputTokens: number): number {
    const pricing = MOCK_PRICING[model] ?? MOCK_PRICING["mock-default"]!;
    return inputTokens * pricing.input + outputTokens * pricing.output;
  }

  // ------------------------------------------------------------------
  // Surface
  // ------------------------------------------------------------------

  async complete(req: LLMProviderRequest): Promise<LLMResponse> {
    const start = Date.now();
    const content = this.next();
    const usedModel = req.model || this.defaultModel;
    const inputTokens = await countTokens(MockProvider.inputText(req.messages));
    const outputTokens = await countTokens(content);
    const durationMs = Date.now() - start;
    return {
      content,
      model: usedModel,
      finishReason: this.finishReason,
      inputTokens,
      outputTokens,
      costUsd: this.estimateCostUsd(usedModel, inputTokens, outputTokens),
      durationMs,
      provider: this.name,
      raw: { mock: true, content },
    };
  }

  async *stream(req: LLMProviderRequest): AsyncGenerator<LLMStreamChunk, void, void> {
    // The mock has no network I/O; we don't bother counting tokens here
    // because the LLMClient stream path synthesises its own response.
    const content = this.next();
    if (!content) {
      yield { delta: "", finishReason: this.finishReason, raw: {} };
      return;
    }
    for (let i = 0; i < content.length; i += this.chunkSize) {
      yield { delta: content.slice(i, i + this.chunkSize), raw: {} };
    }
    // Suppress "unused" hint while keeping a stable surface.
    void req;
    yield { delta: "", finishReason: this.finishReason, raw: {} };
  }
}
