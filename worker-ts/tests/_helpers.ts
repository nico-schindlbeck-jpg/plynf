/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Test helpers — a hand-rolled fetch mock that records requests and
 * returns scripted responses. Mirrors `sdk/typescript/tests/_helpers.ts`
 * — copied so the worker package has zero dev-time coupling to the SDK
 * test layout.
 */

import { vi, type Mock } from "vitest";

import { Plinth } from "@plinth/sdk";

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
      const url =
        input instanceof URL
          ? input.toString()
          : typeof input === "string"
            ? input
            : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      const headers: Record<string, string> = {};
      if (init?.headers) {
        new Headers(init.headers).forEach((v, k) => {
          headers[k] = v;
        });
      }
      const body =
        init?.body == null
          ? null
          : typeof init.body === "string"
            ? init.body
            : await readBody(init.body);
      const request: RecordedRequest = { url, method, headers, body };
      this.requests.push(request);

      for (const route of this.routes) {
        if (route.method === method && route.matcher.test(url)) {
          const init = await route.handler(request);
          return buildResponse(init);
        }
      }
      return new Response(
        JSON.stringify({
          error: { code: "INTERNAL_ERROR", message: `No route for ${method} ${url}` },
        }),
        {
          status: 500,
          headers: { "content-type": "application/json" },
        },
      );
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

// ---------------------------------------------------------------------------
// Plinth client wired to a MockServer
// ---------------------------------------------------------------------------

export function makePlinth(server: MockServer): Plinth {
  return new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    apiKey: "test-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
}

// ---------------------------------------------------------------------------
// JSON fixture builders — mirror `worker/tests/conftest.py`
// ---------------------------------------------------------------------------

const NOW = "2026-01-01T00:00:00Z";

export function makeWorkspaceRecord(opts: { id?: string; name?: string } = {}): unknown {
  return {
    id: opts.id ?? "ws_01TEST",
    name: opts.name ?? "test-ws",
    created_at: NOW,
    updated_at: NOW,
    metadata: {},
  };
}

export function makeWorkflow(opts: {
  id?: string;
  workspaceId?: string;
  name?: string;
  steps?: unknown[];
  status?: string;
  manifest?: string[];
} = {}): unknown {
  return {
    id: opts.id ?? "wf_01TEST",
    workspace_id: opts.workspaceId ?? "ws_01TEST",
    name: opts.name ?? "research",
    steps_manifest: opts.manifest ?? ["search", "fetch"],
    steps: opts.steps ?? [],
    status: opts.status ?? "running",
    metadata: {},
    created_at: NOW,
    started_at: NOW,
    finished_at: null,
  };
}

export function makeStep(opts: {
  id?: string;
  workflowId?: string;
  name?: string;
  status?: string;
  input?: unknown;
  output?: unknown;
} = {}): unknown {
  return {
    id: opts.id ?? "step_01TEST",
    workflow_id: opts.workflowId ?? "wf_01TEST",
    name: opts.name ?? "search",
    status: opts.status ?? "pending",
    attempt: 1,
    started_at: opts.status && opts.status !== "pending" ? NOW : null,
    finished_at:
      opts.status === "completed" || opts.status === "failed" ? NOW : null,
    input: opts.input ?? null,
    output: opts.output ?? null,
    error: null,
    snapshot_id: null,
    created_at: NOW,
  };
}

export function makeLease(opts: {
  stepId?: string;
  workerId?: string;
  status?: "running" | "released" | "expired";
} = {}): unknown {
  return {
    step_id: opts.stepId ?? "step_01TEST",
    worker_id: opts.workerId ?? "worker_01TEST",
    acquired_at: NOW,
    expires_at: "2026-01-01T00:01:00Z",
    heartbeat_at: NOW,
    status: opts.status ?? "running",
  };
}

export function makeWorker(opts: { id?: string; status?: string } = {}): unknown {
  return {
    id: opts.id ?? "worker_01TEST",
    hostname: "test-host",
    pid: 1234,
    started_at: NOW,
    last_heartbeat_at: NOW,
    status: opts.status ?? "active",
  };
}

export function errorEnvelope(code: string, message: string): unknown {
  return { error: { code, message, details: {} } };
}

/**
 * Wire the standard worker-registration routes (register/heartbeat/drain)
 * onto a {@link MockServer}. Tests that don't care about heartbeats can
 * call this in one line.
 */
export function wireWorkerRegistration(server: MockServer): void {
  server.json("POST", /\/v1\/workers\/register$/, makeWorker(), 201);
  server.json("POST", /\/v1\/workers\/worker_01TEST\/heartbeat$/, makeWorker());
  server.json(
    "POST",
    /\/v1\/workers\/worker_01TEST\/drain$/,
    makeWorker({ status: "draining" }),
  );
}
