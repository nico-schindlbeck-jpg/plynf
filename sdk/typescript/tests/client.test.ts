/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import {
  Plinth,
  PlinthError,
  UnauthorizedError,
  WorkspaceNotFoundError,
} from "../src/index.js";
import { MockServer } from "./_helpers.js";

function makeClient(server: MockServer): Plinth {
  return new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    apiKey: "test-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
}

describe("Plinth client construction", () => {
  it("requires apiKey", () => {
    expect(
      () =>
        new Plinth({
          workspaceUrl: "http://w",
          gatewayUrl: "http://g",
          apiKey: "",
        }),
    ).toThrow(/apiKey/);
  });

  it("falls back to localhost defaults when URLs are omitted", () => {
    // Should not throw — workspaceUrl + gatewayUrl have defaults.
    expect(
      () =>
        new Plinth({
          apiKey: "test",
          fetch: (() => new Response()) as unknown as typeof fetch,
        }),
    ).not.toThrow();
  });

  it("does not require identityUrl (it is opt-in for v0.3)", () => {
    const server = new MockServer();
    const client = makeClient(server);
    expect(() => client.identity).toThrow(/identityUrl/);
  });

  it("constructs an identity client when identityUrl is provided", () => {
    const server = new MockServer();
    const client = new Plinth({
      workspaceUrl: "http://w",
      gatewayUrl: "http://g",
      identityUrl: "http://identity.test",
      apiKey: "k",
      fetch: server.fetch as unknown as typeof fetch,
    });
    // Accessing the getter should not throw.
    expect(client.identity).toBeDefined();
  });
});

describe("Plinth client — auth + workspace lookup", () => {
  it("attaches Authorization: Bearer on every request", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces$/, { workspaces: [] });
    server.json("POST", /\/v1\/workspaces$/, {
      id: "ws_1",
      name: "alpha",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      metadata: {},
    });

    const client = makeClient(server);
    await client.workspace("alpha");

    expect(server.requests.length).toBe(2);
    for (const req of server.requests) {
      expect(req.headers.authorization).toBe("Bearer test-token");
    }
  });

  it("get-or-creates a workspace by name", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces$/, { workspaces: [] });
    server.on("POST", /\/v1\/workspaces$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.name).toBe("research");
      return {
        status: 201,
        body: {
          id: "ws_abc",
          name: body.name,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          metadata: {},
        },
      };
    });

    const client = makeClient(server);
    const ws = await client.workspace("research");
    expect(ws.id).toBe("ws_abc");
    expect(ws.name).toBe("research");
  });

  it("returns existing workspace when name matches", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces$/, {
      workspaces: [
        {
          id: "ws_existing",
          name: "research",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          metadata: {},
        },
      ],
    });

    const client = makeClient(server);
    const ws = await client.workspace("research");
    expect(ws.id).toBe("ws_existing");
    // Should NOT have called POST
    expect(server.requests.find((r) => r.method === "POST")).toBeUndefined();
  });

  it("maps 404 with WORKSPACE_NOT_FOUND code to typed error", async () => {
    const server = new MockServer();
    server.on("GET", /\/v1\/workspaces\/ws_missing$/, () => ({
      status: 404,
      body: {
        error: {
          code: "WORKSPACE_NOT_FOUND",
          message: "Workspace ws_missing does not exist",
        },
      },
    }));

    const client = makeClient(server);
    await expect(client.getWorkspace("ws_missing")).rejects.toBeInstanceOf(WorkspaceNotFoundError);
    await expect(client.getWorkspace("ws_missing")).rejects.toMatchObject({
      code: "WORKSPACE_NOT_FOUND",
      status: 404,
    });
  });

  it("maps 401 to UnauthorizedError even without an envelope code", async () => {
    const server = new MockServer();
    server.on("GET", /\/v1\/workspaces$/, () => ({ status: 401, body: "" }));

    const client = makeClient(server);
    await expect(client.listWorkspaces()).rejects.toBeInstanceOf(UnauthorizedError);
  });
});

describe("Plinth — token counting", () => {
  it("countTokens returns a number for non-empty input", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    const n = await client.countTokens("Hello world");
    expect(typeof n).toBe("number");
    expect(n).toBeGreaterThan(0);
  });

  it("countTokens returns 0 for empty input", async () => {
    const server = new MockServer();
    const client = makeClient(server);
    await expect(client.countTokens("")).resolves.toBe(0);
  });

  it("estimateCost returns a USD number", () => {
    const server = new MockServer();
    const client = makeClient(server);
    expect(client.estimateCost(1000, 0)).toBeGreaterThan(0);
    expect(client.estimateCost(0)).toBe(0);
  });
});

describe("Plinth — withAgent helper", () => {
  it("withAgent passes a workspace + tools context", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces$/, {
      workspaces: [
        {
          id: "ws_agent",
          name: "agent-ws",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          metadata: {},
        },
      ],
    });

    const client = makeClient(server);
    const result = await client.withAgent("researcher", "agent-ws", (ctx) => {
      expect(ctx.agentId).toBe("researcher");
      expect(ctx.workspace.id).toBe("ws_agent");
      expect(ctx.tools).toBe(client.tools);
      return 42;
    });
    expect(result).toBe(42);
  });
});

describe("Plinth — error class hierarchy", () => {
  it("PlinthError is the supertype of every typed error", () => {
    const wrapped = new WorkspaceNotFoundError("missing", 404);
    expect(wrapped).toBeInstanceOf(PlinthError);
    expect(wrapped).toBeInstanceOf(Error);
    expect(wrapped.code).toBe("WORKSPACE_NOT_FOUND");
  });
});
