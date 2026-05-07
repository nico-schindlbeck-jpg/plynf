/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import { InvalidArgumentsError, Plinth, ToolNotFoundError } from "../src/index.js";
import { MockServer, parseQuery } from "./_helpers.js";

function makeClient(server: MockServer): Plinth {
  return new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    apiKey: "test-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
}

describe("ToolsClient", () => {
  it("invokes a tool and returns the gateway response", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/invoke$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.tool_id).toBe("web.fetch");
      expect(body.arguments).toEqual({ url: "https://example.com" });
      return {
        body: {
          tool_id: "web.fetch",
          arguments: body.arguments,
          result: { content: "<!doctype html>", status: 200, content_type: "text/html" },
          cached: false,
          duration_ms: 42,
          audit_id: "evt_xyz",
          cost_estimate_usd: 0.0,
        },
      };
    });

    const client = makeClient(server);
    const res = await client.tools.invoke("web.fetch", { url: "https://example.com" });
    expect(res.tool_id).toBe("web.fetch");
    expect(res.cached).toBe(false);
    expect((res.result as { status: number }).status).toBe(200);
  });

  it("forwards workspace_id and agent_id for audit attribution", async () => {
    const server = new MockServer();
    let captured: Record<string, unknown> = {};
    server.on("POST", /\/v1\/invoke$/, (req) => {
      captured = JSON.parse(req.body ?? "{}");
      return {
        body: {
          tool_id: "web.fetch",
          arguments: {},
          result: null,
          cached: false,
          duration_ms: 1,
          audit_id: "evt_a",
          cost_estimate_usd: 0,
        },
      };
    });

    const client = makeClient(server);
    await client.tools.invoke(
      "web.fetch",
      { url: "u" },
      { workspaceId: "ws_1", agentId: "agent_1", cache: false, idempotencyKey: "k1" },
    );
    expect(captured).toMatchObject({
      tool_id: "web.fetch",
      workspace_id: "ws_1",
      agent_id: "agent_1",
      cache: false,
      idempotency_key: "k1",
    });
  });

  it("dryRun POSTs to /v1/invoke/dry-run", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/invoke\/dry-run$/, () => ({
      body: {
        tool_id: "web.fetch",
        arguments: {},
        would_invoke: false,
        cached_result: { content: "" },
        estimated_cost_usd: 0,
        estimated_duration_ms: 0,
      },
    }));

    const client = makeClient(server);
    const res = await client.tools.dryRun("web.fetch", { url: "x" });
    expect(res.would_invoke).toBe(false);
    expect(res.cached_result).toEqual({ content: "" });
  });

  it("register POSTs the tool registration body", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tools\/register$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      return {
        status: 201,
        body: {
          ...body,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      };
    });

    const client = makeClient(server);
    const tool = await client.tools.register({
      tool_id: "test.echo",
      name: "echo",
      description: "echoes input",
      transport: "http",
      endpoint: "http://localhost:9999",
      input_schema: {},
      output_schema: {},
    });
    expect(tool.tool_id).toBe("test.echo");
    expect(tool.created_at).toBe("2026-01-01T00:00:00Z");
  });

  it("list returns the tools array", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/tools$/, {
      tools: [
        {
          tool_id: "web.fetch",
          name: "fetch",
          description: "",
          transport: "http",
          endpoint: "x",
          input_schema: {},
          output_schema: {},
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
    });

    const client = makeClient(server);
    const tools = await client.tools.list();
    expect(tools).toHaveLength(1);
    expect(tools[0]!.tool_id).toBe("web.fetch");
  });

  it("audit forwards query parameters", async () => {
    const server = new MockServer();
    server.on("GET", /\/v1\/audit/, (req) => {
      const q = parseQuery(req.url);
      expect(q.workspace_id).toBe("ws_1");
      expect(q.since).toBe("1h");
      return { body: { events: [] } };
    });

    const client = makeClient(server);
    const events = await client.tools.audit({ workspaceId: "ws_1", since: "1h" });
    expect(events).toEqual([]);
  });

  it("maps TOOL_NOT_FOUND to typed error", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/invoke$/, () => ({
      status: 404,
      body: { error: { code: "TOOL_NOT_FOUND", message: "no such tool" } },
    }));
    const client = makeClient(server);
    await expect(client.tools.invoke("missing", {})).rejects.toBeInstanceOf(ToolNotFoundError);
  });

  it("maps INVALID_ARGUMENTS to typed error", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/invoke$/, () => ({
      status: 400,
      body: { error: { code: "INVALID_ARGUMENTS", message: "missing url", details: { field: "url" } } },
    }));
    const client = makeClient(server);
    const err = await client.tools
      .invoke("web.fetch", {})
      .catch((e: unknown) => e as InvalidArgumentsError);
    expect(err).toBeInstanceOf(InvalidArgumentsError);
    expect((err as InvalidArgumentsError).details).toEqual({ field: "url" });
  });
});
