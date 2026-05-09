/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Tests for v1.0 SDK additions: tenant quotas + usage.
 */

import { describe, expect, it } from "vitest";

import { Plinth, type TenantQuotas, type TenantUsage } from "../src/index.js";
import { MockServer } from "./_helpers.js";


function makeClient(server: MockServer): Plinth {
  return new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    identityUrl: "http://identity.test",
    apiKey: "bootstrap-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
}

const fullQuotas: TenantQuotas = {
  tenant_id: "acme",
  max_workspaces: 100,
  max_storage_gb: 10.0,
  max_channels_per_workspace: 50,
  max_workflows_per_workspace: 100,
  max_active_tokens: 1000,
  max_oauth_connections: 50,
  max_cost_usd_day: 100.0,
  max_cost_usd_month: 2000.0,
  max_invocations_per_minute: 600,
  updated_at: "2026-01-01T00:00:00Z",
};

describe("IdentityClient — quotas (v1.0)", () => {
  it("getQuotas returns the full envelope", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/tenants\/acme\/quotas$/, fullQuotas);
    const client = makeClient(server);
    const q = await client.identity!.getQuotas("acme");
    expect(q.max_workspaces).toBe(100);
    expect(q.max_cost_usd_day).toBe(100.0);
  });

  it("setQuotas POSTs the partial body and returns the envelope", async () => {
    const server = new MockServer();
    let captured: Record<string, unknown> = {};
    server.on("POST", /\/v1\/tenants\/acme\/quotas$/, (req) => {
      captured = JSON.parse(req.body ?? "{}");
      return { status: 200, body: { ...fullQuotas, max_workspaces: 7 } };
    });
    const client = makeClient(server);
    const q = await client.identity!.setQuotas("acme", { max_workspaces: 7 });
    expect(q.max_workspaces).toBe(7);
    expect(captured).toEqual({ max_workspaces: 7 });
  });

  it("resetQuotas calls DELETE", async () => {
    const server = new MockServer();
    let called = false;
    server.on("DELETE", /\/v1\/tenants\/acme\/quotas$/, () => {
      called = true;
      return { status: 204 };
    });
    const client = makeClient(server);
    await client.identity!.resetQuotas("acme");
    expect(called).toBe(true);
  });

  it("getUsage returns the rollup", async () => {
    const usage: TenantUsage = {
      tenant_id: "acme",
      workspaces: 0,
      storage_gb: 0,
      active_tokens: 4,
      oauth_connections: 0,
      cost_usd_day: 0,
      cost_usd_month: 0,
      last_invocation_at: null,
      notes: { workspaces: "owned by workspace service" },
    };
    const server = new MockServer();
    server.json("GET", /\/v1\/tenants\/acme\/usage$/, usage);
    const client = makeClient(server);
    const u = await client.identity!.getUsage("acme");
    expect(u.active_tokens).toBe(4);
    expect(u.notes.workspaces).toContain("workspace");
  });

  it("getQuotas defaults shape passes through", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/tenants\/none\/quotas$/, {
      ...fullQuotas,
      tenant_id: "none",
      max_workspaces: 100,
    });
    const client = makeClient(server);
    const q = await client.identity!.getQuotas("none");
    expect(q.tenant_id).toBe("none");
  });

  it("setQuotas includes empty update body for full overwrite", async () => {
    const server = new MockServer();
    let captured: Record<string, unknown> = {};
    server.on("POST", /\/v1\/tenants\/acme\/quotas$/, (req) => {
      captured = JSON.parse(req.body ?? "{}");
      return { status: 200, body: fullQuotas };
    });
    const client = makeClient(server);
    await client.identity!.setQuotas("acme", {});
    expect(captured).toEqual({});
  });
});
