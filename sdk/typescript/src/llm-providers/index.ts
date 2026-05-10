/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Built-in LLM providers for the Plinth SDK.
 *
 * Each provider is a small adapter wrapping a vendor SDK behind the
 * {@link LLMProvider} interface. They are loaded lazily from
 * {@link buildProvider} so the optional peer dependencies
 * (`@anthropic-ai/sdk`, `openai`) only get pulled in when actually used.
 *
 * Adding a new provider is intentionally a single file change:
 *
 *   1. Add `llm-providers/<name>.ts` with a `Provider` class implementing
 *      the {@link LLMProvider} interface.
 *   2. Wire it into {@link buildProvider} below.
 *   3. Optionally add a pricing table for the cost helper.
 *
 * The {@link MockProvider} ships unconditionally — tests rely on it.
 */

import type { LLMProviderName } from "../types.js";
import { AnthropicProvider, type AnthropicProviderConfig } from "./anthropic.js";
import { MockProvider, type MockProviderConfig } from "./mock.js";
import { OpenAIProvider, type OpenAIProviderConfig } from "./openai.js";
import type { LLMProvider } from "./types.js";

/**
 * Construct one of the built-in providers by name.
 *
 * Async because the vendor SDKs are loaded lazily — see
 * {@link AnthropicProvider.create} / {@link OpenAIProvider.create}.
 */
export async function buildProvider(
  name: LLMProviderName | string,
  config: Record<string, unknown> = {},
): Promise<LLMProvider> {
  const lower = name.toLowerCase();
  if (lower === "mock") {
    return new MockProvider(config as MockProviderConfig);
  }
  if (lower === "anthropic") {
    return await AnthropicProvider.create(config as AnthropicProviderConfig);
  }
  if (lower === "openai") {
    return await OpenAIProvider.create(config as OpenAIProviderConfig);
  }
  throw new Error(
    `Unknown LLM provider ${JSON.stringify(name)}. ` +
      `Built-ins: 'anthropic', 'openai', 'mock'.`,
  );
}

export { AnthropicProvider, MockProvider, OpenAIProvider };
export { ANTHROPIC_PRICING } from "./anthropic.js";
export { MOCK_PRICING } from "./mock.js";
export { OPENAI_PRICING } from "./openai.js";
export type { AnthropicProviderConfig } from "./anthropic.js";
export type { MockProviderConfig } from "./mock.js";
export type { OpenAIProviderConfig } from "./openai.js";
export type { LLMProvider, LLMProviderRequest } from "./types.js";
