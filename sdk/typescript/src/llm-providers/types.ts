/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Shared types between the provider implementations and the dispatcher.
 *
 * Lives next to the provider files so a third-party adapter can implement
 * {@link LLMProvider} without dragging in the rest of the SDK.
 */

import type { LLMMessage, LLMResponse, LLMStreamChunk } from "../types.js";

/**
 * The minimum surface a Plinth LLM provider must implement.
 *
 * Each provider is a thin adapter over its vendor SDK; the
 * {@link LLMClient} owns retry, audit, and request-shape normalization.
 */
export interface LLMProvider {
  readonly name: string;

  /** Run a one-shot completion. */
  complete(req: LLMProviderRequest): Promise<LLMResponse>;

  /**
   * Stream a completion as a sequence of chunks.
   *
   * The provider is responsible for emitting at least one terminal
   * chunk carrying `finishReason` so the caller can stop without
   * inspecting `raw`.
   */
  stream(req: LLMProviderRequest): AsyncGenerator<LLMStreamChunk, void, void>;

  /** Compute USD cost given the model + token usage reported by the API. */
  estimateCostUsd(model: string, inputTokens: number, outputTokens: number): number;
}

/**
 * Inbound request to a provider.
 *
 * Mirrors {@link LLMRequest} but with the provider-only fields the
 * adapter cares about (workspace/agent ids are stripped — they're audit
 * concerns, not provider concerns).
 */
export interface LLMProviderRequest {
  model: string;
  messages: LLMMessage[];
  maxTokens?: number;
  temperature?: number;
  topP?: number;
  stopSequences?: string[];
  /** Provider-specific extras passed through verbatim. */
  extra?: Record<string, unknown>;
}
