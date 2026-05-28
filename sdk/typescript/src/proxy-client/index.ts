/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Plynf proxy client SDK (TypeScript).
 *
 * Two entry points:
 *
 *   import { PlynfOpenAI } from "@plinth/sdk/proxy-client";
 *   const client = new PlynfOpenAI({ apiKey: "sk-…", plynfUrl: "https://app.plynf.com" });
 *   await client.chat.completions.create({ model: "gpt-4o", messages: [...] });
 *
 *   import { wrapTool } from "@plinth/sdk/proxy-client";
 *   const shaped = wrapTool(myToolFn, { plynfUrl, apiKey });
 */

export { PlynfOpenAI, PlynfProxyError } from "./openai-drop-in.js";
export { wrapTool, wrapTools, ShapeError } from "./tools-wrap.js";
export type {
  ChatCompletionRequest,
  ChatCompletionResponse,
  ChatCompletionChunk,
  ToolWrapper,
} from "./types.js";
