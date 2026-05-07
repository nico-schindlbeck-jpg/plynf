/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Test helpers — a hand-rolled fetch mock that records requests and
 * returns scripted responses. Avoids pulling msw into the SDK.
 */

import { vi, type Mock } from "vitest";

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

export interface MockResponseInit {
  status?: number;
  body?: unknown;
  bodyBytes?: Uint8Array;
  headers?: Record<string, string>;
}

export type Handler = (req: RecordedRequest) => MockResponseInit | Promise<MockResponseInit>;

interface Route {
  method: string;
  matcher: RegExp;
  handler: Handler;
}

/** Tiny route-based fetch mock. */
export class MockServer {
  readonly fetch: Mock;
  readonly requests: RecordedRequest[] = [];
  private readonly routes: Route[] = [];

  constructor() {
    this.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof URL ? input.toString() : typeof input === "string" ? input : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      const headers: Record<string, string> = {};
      if (init?.headers) {
        new Headers(init.headers).forEach((v, k) => {
          headers[k] = v;
        });
      }
      const body = init?.body == null ? null : typeof init.body === "string" ? init.body : await readBody(init.body);
      const request: RecordedRequest = { url, method, headers, body };
      this.requests.push(request);

      for (const route of this.routes) {
        if (route.method === method && route.matcher.test(url)) {
          const init = await route.handler(request);
          return buildResponse(init);
        }
      }
      return new Response(JSON.stringify({ error: { code: "INTERNAL_ERROR", message: `No route for ${method} ${url}` } }), {
        status: 500,
        headers: { "content-type": "application/json" },
      });
    });
  }

  on(method: string, matcher: RegExp, handler: Handler): this {
    this.routes.push({ method: method.toUpperCase(), matcher, handler });
    return this;
  }

  /** Convenience: respond with a JSON body for a given route. */
  json(method: string, matcher: RegExp, body: unknown, status = 200): this {
    return this.on(method, matcher, () => ({ status, body }));
  }
}

async function readBody(body: BodyInit): Promise<string> {
  if (typeof body === "string") return body;
  if (body instanceof Uint8Array) return new TextDecoder().decode(body);
  if (body instanceof ArrayBuffer) return new TextDecoder().decode(body);
  if (body instanceof Blob) return await body.text();
  // Fallback: best-effort stringify.
  return String(body);
}

function buildResponse(init: MockResponseInit): Response {
  const status = init.status ?? 200;
  const headers = new Headers(init.headers ?? {});
  if (init.bodyBytes !== undefined) {
    if (!headers.has("content-type")) headers.set("content-type", "application/octet-stream");
    return new Response(init.bodyBytes, { status, headers });
  }
  if (init.body === undefined) {
    return new Response(null, { status, headers });
  }
  if (typeof init.body === "string") {
    if (!headers.has("content-type")) headers.set("content-type", "text/plain; charset=utf-8");
    return new Response(init.body, { status, headers });
  }
  if (!headers.has("content-type")) headers.set("content-type", "application/json");
  return new Response(JSON.stringify(init.body), { status, headers });
}

/** Convenience matcher for `parseQuery(req.url)`. */
export function parseQuery(url: string): Record<string, string> {
  const u = new URL(url);
  const out: Record<string, string> = {};
  u.searchParams.forEach((v, k) => {
    out[k] = v;
  });
  return out;
}
