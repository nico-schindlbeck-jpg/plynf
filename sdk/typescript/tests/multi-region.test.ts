/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Tests for the v1.0 multi-region failover behaviour in the TS SDK.
 */

import { describe, expect, it } from "vitest";

import { Plinth, PlinthError } from "../src/index.js";
import { HttpClient } from "../src/http.js";
import { MockServer } from "./_helpers.js";

const WORKSPACE_PRIMARY = "http://workspace-eu.test";
const WORKSPACE_FALLBACK = "http://workspace-us.test";
const GATEWAY_PRIMARY = "http://gateway-eu.test";
const GATEWAY_FALLBACK = "http://gateway-us.test";

describe("Plinth multi-region — config", () => {
  it("accepts region + fallbackRegions parameters", () => {
    const server = new MockServer();
    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu-west-1",
      fallbackRegions: ["us-east-1"],
      fallbackWorkspaceUrls: { "us-east-1": WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { "us-east-1": GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    // No throw on construction is the contract.
    expect(client).toBeDefined();
  });

  it("drops fallback regions without a matching URL", () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces$/, { workspaces: [] });

    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu",
      fallbackRegions: ["us", "ap"],
      // Only ``us`` has a workspace URL — ``ap`` is silently dropped.
      fallbackWorkspaceUrls: { us: WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { us: GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    expect(client).toBeDefined();
  });

  it("HttpClient.candidates() returns deterministic order", () => {
    const server = new MockServer();
    const http = new HttpClient({
      baseUrl: WORKSPACE_PRIMARY,
      apiKey: "k",
      defaultTimeoutMs: 1000,
      fetch: server.fetch as unknown as typeof fetch,
      fallbackUrls: {
        "us-east-1": WORKSPACE_FALLBACK,
        "ap-south-1": "http://workspace-ap.test",
      },
      primaryRegion: "eu-west-1",
    });
    const candidates = http.candidates();
    expect(candidates.map(([r]) => r)).toEqual([
      "eu-west-1",
      "us-east-1",
      "ap-south-1",
    ]);
  });
});

describe("Plinth multi-region — failover behaviour", () => {
  it("retries fallback on connection error", async () => {
    const server = new MockServer();
    let primaryHits = 0;
    let fallbackHits = 0;
    server.on("GET", new RegExp(`^${WORKSPACE_PRIMARY}/`), () => {
      primaryHits++;
      throw new Error("ECONNREFUSED");
    });
    server.json("GET", new RegExp(`^${WORKSPACE_FALLBACK}/`), { workspaces: [] });
    server.fetch.mockImplementation((async (input: any, init: any) => {
      const url = typeof input === "string" ? input : input.url;
      // Manually run because we override mockImplementation.
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.startsWith(WORKSPACE_PRIMARY)) {
        primaryHits++;
        throw new Error("ECONNREFUSED");
      }
      if (url.startsWith(WORKSPACE_FALLBACK)) {
        fallbackHits++;
        return new Response(JSON.stringify({ workspaces: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu",
      fallbackRegions: ["us"],
      fallbackWorkspaceUrls: { us: WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { us: GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    const result = await client.listWorkspaces();
    expect(result).toEqual([]);
    expect(primaryHits).toBeGreaterThanOrEqual(1);
    expect(fallbackHits).toBe(1);
  });

  it("retries fallback on 503", async () => {
    const server = new MockServer();
    let primaryHits = 0;
    let fallbackHits = 0;
    server.fetch.mockImplementation((async (input: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith(WORKSPACE_PRIMARY)) {
        primaryHits++;
        return new Response("overloaded", { status: 503 });
      }
      if (url.startsWith(WORKSPACE_FALLBACK)) {
        fallbackHits++;
        return new Response(JSON.stringify({ workspaces: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu",
      fallbackRegions: ["us"],
      fallbackWorkspaceUrls: { us: WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { us: GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    await client.listWorkspaces();
    expect(primaryHits).toBe(1);
    expect(fallbackHits).toBe(1);
  });

  it("redirects on 409 with X-Plinth-Primary-Region", async () => {
    const server = new MockServer();
    let primaryHits = 0;
    let fallbackHits = 0;
    server.fetch.mockImplementation((async (input: any, init: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith(WORKSPACE_PRIMARY)) {
        primaryHits++;
        return new Response(
          JSON.stringify({
            error: { code: "REPLICA_READ_ONLY", message: "go elsewhere", details: {} },
          }),
          {
            status: 409,
            headers: {
              "content-type": "application/json",
              "X-Plinth-Primary-Region": "us",
            },
          },
        );
      }
      if (url.startsWith(WORKSPACE_FALLBACK)) {
        fallbackHits++;
        return new Response(
          JSON.stringify({
            id: "ws_1",
            name: "x",
            metadata: {},
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu",
      fallbackRegions: ["us"],
      fallbackWorkspaceUrls: { us: WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { us: GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    // ``getWorkspace`` for an unknown id triggers a 404 but POST sees the 409 path.
    // We use ``workspace()`` which falls through to a POST.
    server.fetch.mockImplementation((async (input: any, init: any) => {
      const url = typeof input === "string" ? input : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url.includes("/v1/workspaces") && !url.includes("/ws_")) {
        // First listWorkspaces() call from .workspace(name)
        if (url.startsWith(WORKSPACE_PRIMARY)) {
          return new Response(
            JSON.stringify({
              error: { code: "REPLICA_READ_ONLY", message: "go", details: {} },
            }),
            {
              status: 409,
              headers: {
                "content-type": "application/json",
                "X-Plinth-Primary-Region": "us",
              },
            },
          );
        }
        if (url.startsWith(WORKSPACE_FALLBACK)) {
          return new Response(JSON.stringify({ workspaces: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
      }
      if (method === "POST" && url.includes("/v1/workspaces")) {
        if (url.startsWith(WORKSPACE_PRIMARY)) {
          primaryHits++;
          return new Response(
            JSON.stringify({
              error: { code: "REPLICA_READ_ONLY", message: "go", details: {} },
            }),
            {
              status: 409,
              headers: {
                "content-type": "application/json",
                "X-Plinth-Primary-Region": "us",
              },
            },
          );
        }
        if (url.startsWith(WORKSPACE_FALLBACK)) {
          fallbackHits++;
          return new Response(
            JSON.stringify({
              id: "ws_1",
              name: "x",
              metadata: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            }),
            { status: 201, headers: { "content-type": "application/json" } },
          );
        }
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const ws = await client.workspace("x");
    expect(ws).toBeDefined();
    expect(primaryHits).toBeGreaterThan(0);
    expect(fallbackHits).toBeGreaterThan(0);
  });

  it("surfaces 4xx errors without retrying", async () => {
    const server = new MockServer();
    let primaryHits = 0;
    let fallbackHits = 0;
    server.fetch.mockImplementation((async (input: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith(WORKSPACE_PRIMARY)) {
        primaryHits++;
        return new Response(
          JSON.stringify({
            error: { code: "WORKSPACE_NOT_FOUND", message: "no", details: {} },
          }),
          { status: 404, headers: { "content-type": "application/json" } },
        );
      }
      if (url.startsWith(WORKSPACE_FALLBACK)) {
        fallbackHits++;
        return new Response(JSON.stringify({ id: "x" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu",
      fallbackRegions: ["us"],
      fallbackWorkspaceUrls: { us: WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { us: GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    await expect(client.getWorkspace("ws_x")).rejects.toThrow();
    expect(primaryHits).toBe(1);
    expect(fallbackHits).toBe(0);
  });

  it("raises an error when no fallback succeeds", async () => {
    const server = new MockServer();
    server.fetch.mockImplementation((async () => {
      throw new Error("ECONNREFUSED");
    }) as any);

    const http = new HttpClient({
      baseUrl: WORKSPACE_PRIMARY,
      apiKey: "k",
      defaultTimeoutMs: 1000,
      fetch: server.fetch as unknown as typeof fetch,
      fallbackUrls: { us: WORKSPACE_FALLBACK },
      primaryRegion: "eu",
    });
    await expect(
      http.requestJson({ method: "GET", path: "/v1/workspaces" }),
    ).rejects.toThrow();
  });

  it("works without fallback config (back-compat)", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces$/, { workspaces: [] });
    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      fetch: server.fetch as unknown as typeof fetch,
    });
    const result = await client.listWorkspaces();
    expect(result).toEqual([]);
  });

  it("redirects on 421 (Misdirected Request)", async () => {
    const server = new MockServer();
    let primaryHits = 0;
    let fallbackHits = 0;
    server.fetch.mockImplementation((async (input: any, init: any) => {
      const url = typeof input === "string" ? input : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "POST" && url.startsWith(WORKSPACE_PRIMARY)) {
        primaryHits++;
        return new Response(
          JSON.stringify({
            error: { code: "REPLICA_READ_ONLY", message: "go", details: {} },
          }),
          {
            status: 421,
            headers: {
              "content-type": "application/json",
              "X-Plinth-Primary-Region": "us",
              "X-Plinth-Primary-URL": WORKSPACE_FALLBACK,
            },
          },
        );
      }
      if (method === "POST" && url.startsWith(WORKSPACE_FALLBACK)) {
        fallbackHits++;
        return new Response(
          JSON.stringify({
            id: "ws_1",
            name: "x",
            metadata: {},
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        );
      }
      // GETs (e.g. listWorkspaces during ``workspace(name)``) succeed locally.
      if (method === "GET" && url.startsWith(WORKSPACE_PRIMARY)) {
        return new Response(JSON.stringify({ workspaces: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const client = new Plinth({
      apiKey: "k",
      workspaceUrl: WORKSPACE_PRIMARY,
      gatewayUrl: GATEWAY_PRIMARY,
      region: "eu",
      fallbackRegions: ["us"],
      fallbackWorkspaceUrls: { us: WORKSPACE_FALLBACK },
      fallbackGatewayUrls: { us: GATEWAY_FALLBACK },
      fetch: server.fetch as unknown as typeof fetch,
    });
    const ws = await client.workspace("x");
    expect(ws).toBeDefined();
    expect(primaryHits).toBeGreaterThan(0);
    expect(fallbackHits).toBeGreaterThan(0);
  });

  it("rejects untrusted X-Plinth-Primary-URL hints", async () => {
    const server = new MockServer();
    server.fetch.mockImplementation((async (input: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith(WORKSPACE_PRIMARY)) {
        return new Response(
          JSON.stringify({
            error: { code: "REPLICA_READ_ONLY", message: "go", details: {} },
          }),
          {
            status: 421,
            headers: {
              "content-type": "application/json",
              "X-Plinth-Primary-Region": "evil",
              // URL not in fallbacks — must be rejected.
              "X-Plinth-Primary-URL": "http://attacker.example",
            },
          },
        );
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const http = new HttpClient({
      baseUrl: WORKSPACE_PRIMARY,
      apiKey: "k",
      defaultTimeoutMs: 1000,
      fetch: server.fetch as unknown as typeof fetch,
      fallbackUrls: { us: WORKSPACE_FALLBACK },
      primaryRegion: "eu",
    });
    await expect(
      http.requestJson({
        method: "POST",
        path: "/v1/workspaces",
        json: { name: "x" },
      }),
    ).rejects.toThrow();
  });

  it("does not loop when replicas bounce 421s at each other", async () => {
    const server = new MockServer();
    let primaryHits = 0;
    let fallbackHits = 0;
    server.fetch.mockImplementation((async (input: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith(WORKSPACE_PRIMARY)) {
        primaryHits++;
        return new Response(
          JSON.stringify({ error: { code: "REPLICA_READ_ONLY", message: "go", details: {} } }),
          {
            status: 421,
            headers: { "content-type": "application/json", "X-Plinth-Primary-Region": "us" },
          },
        );
      }
      if (url.startsWith(WORKSPACE_FALLBACK)) {
        fallbackHits++;
        // Bounce back at the original primary.
        return new Response(
          JSON.stringify({ error: { code: "REPLICA_READ_ONLY", message: "go", details: {} } }),
          {
            status: 421,
            headers: { "content-type": "application/json", "X-Plinth-Primary-Region": "eu" },
          },
        );
      }
      return new Response("nope", { status: 404 });
    }) as any);

    const http = new HttpClient({
      baseUrl: WORKSPACE_PRIMARY,
      apiKey: "k",
      defaultTimeoutMs: 1000,
      fetch: server.fetch as unknown as typeof fetch,
      fallbackUrls: { us: WORKSPACE_FALLBACK },
      primaryRegion: "eu",
    });
    await expect(
      http.requestJson({
        method: "POST",
        path: "/v1/workspaces",
        json: { name: "x" },
      }),
    ).rejects.toThrow();
    // Each URL is hit exactly once; no infinite loop.
    expect(primaryHits).toBe(1);
    expect(fallbackHits).toBe(1);
  });
});
