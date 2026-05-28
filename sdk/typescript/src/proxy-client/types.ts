/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Wire-format types for the Plynf proxy. Only the surfaces we actually
 * implement are typed — the rest is deliberately `unknown` so future
 * additions don't break callers.
 */

export interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
}

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ChatCompletionRequest {
  model: string;
  messages: ChatMessage[];
  tools?: unknown[];
  tool_choice?: unknown;
  stream?: boolean;
  response_format?: unknown;
  temperature?: number;
  max_tokens?: number;
  [k: string]: unknown;
}

export interface ChatCompletionChoice {
  index: number;
  finish_reason: string | null;
  message: ChatMessage;
}

export interface ChatCompletionResponse {
  id: string;
  object: "chat.completion";
  created: number;
  model: string;
  choices: ChatCompletionChoice[];
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens?: number };
}

export interface ChatCompletionChunk {
  id: string;
  object: "chat.completion.chunk";
  created: number;
  model: string;
  choices: Array<{
    index: number;
    delta: Partial<ChatMessage>;
    finish_reason: string | null;
  }>;
}

export type ToolWrapper<F extends (...args: unknown[]) => unknown> = F & {
  __plynfWrapped: true;
  __plynfToolName: string;
};
