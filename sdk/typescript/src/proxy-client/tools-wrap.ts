/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Client-side tool wrapping.
 *
 * For frameworks that execute tools locally (LangChain.js, Vercel AI SDK,
 * custom Node.js agents), `wrapTool` runs your function and then POSTs the
 * raw response to Plynf's /v1/shape endpoint so the LLM only sees the
 * shaped version.
 *
 * Fail-open: if Plynf is unreachable, the agent still gets the raw
 * response. Network resilience > strict policy enforcement at this layer.
 */

import type { ToolWrapper } from "./types.js";

export class ShapeError extends Error {
  constructor(public status: number, public body: string) {
    super(`Plynf shape returned ${status}: ${body.slice(0, 300)}`);
    this.name = "ShapeError";
  }
}

export interface WrapToolOptions {
  plynfUrl: string;
  apiKey: string;
  toolName?: string;
  tenantId?: string;
  timeoutMs?: number;
  fetch?: typeof fetch;
}

interface ShapeResponse {
  shaped: unknown;
  shaped_by_plynf: boolean;
  raw_response_tokens?: number;
  shaped_response_tokens?: number;
  saved_tokens?: number;
  savings_pct?: number;
}

async function postShape(
  opts: Required<Pick<WrapToolOptions, "plynfUrl" | "apiKey">> &
    Pick<WrapToolOptions, "tenantId" | "timeoutMs" | "fetch">,
  tool: string,
  raw: unknown,
): Promise<unknown> {
  const fetchImpl = opts.fetch ?? fetch;
  const url = `${opts.plynfUrl.replace(/\/+$/, "")}/v1/shape`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 30_000);
  try {
    const resp = await fetchImpl(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${opts.apiKey}`,
      },
      body: JSON.stringify({
        tool,
        raw_response: raw,
        tenant_id: opts.tenantId,
      }),
      signal: controller.signal,
    });
    if (!resp.ok) {
      throw new ShapeError(resp.status, await resp.text());
    }
    const data = (await resp.json()) as ShapeResponse;
    return data.shaped ?? raw;
  } finally {
    clearTimeout(timer);
  }
}

export function wrapTool<F extends (...args: unknown[]) => unknown | Promise<unknown>>(
  fn: F,
  opts: WrapToolOptions,
): ToolWrapper<F> {
  const toolName = opts.toolName ?? fn.name ?? "anonymous_tool";

  const wrapped = (async (...args: Parameters<F>) => {
    const raw = await fn(...(args as unknown[]));
    try {
      return await postShape(opts, toolName, raw);
    } catch {
      // Fail-open — agent still works on the raw response.
      return raw;
    }
  }) as ToolWrapper<F>;

  // Object.defineProperty preserves `name` and adds metadata.
  Object.defineProperty(wrapped, "name", { value: toolName });
  (wrapped as unknown as { __plynfWrapped: true }).__plynfWrapped = true;
  (wrapped as unknown as { __plynfToolName: string }).__plynfToolName = toolName;
  return wrapped;
}

export function wrapTools<F extends (...args: unknown[]) => unknown | Promise<unknown>>(
  fns: F[],
  opts: WrapToolOptions,
): ToolWrapper<F>[] {
  return fns.map((fn) => wrapTool(fn, opts));
}
