/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Tests for ``LocksClient`` (v0.6 generic locks). All tests run offline
 * against the in-process ``MockServer`` from ``_helpers.ts`` so we
 * exercise the request shapes the SDK emits without a real workspace
 * process running.
 */

import { describe, expect, it } from "vitest";

import {
  LockConflictError,
  LockNotFoundError,
  LockNotHeldError,
  Plinth,
  type Lock,
  type Workspace,
} from "../src/index.js";
import { MockServer } from "./_helpers.js";

async function bootstrap(server: MockServer): Promise<{ ws: Workspace }> {
  server.json("GET", /\/v1\/workspaces$/, {
    workspaces: [
      {
        id: "ws_1",
        name: "alpha",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        metadata: {},
      },
    ],
  });
  const client = new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    apiKey: "test-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
  const ws = await client.workspace("alpha");
  return { ws };
}

function makeLock(overrides: Partial<Lock> = {}): Lock {
  return {
    name: "kv:sources/index",
    workspace_id: "ws_1",
    holder: "agent-A",
    acquired_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-01T00:01:00Z",
    heartbeat_at: "2026-01-01T00:00:00Z",
    waiters: 0,
    ...overrides,
  };
}

describe("LocksClient", () => {
  it("acquire posts holder/ttl/wait_ms and returns Lock", async () => {
    const server = new MockServer();
    server.on("POST", /\/locks\/kv:sources\/index\/acquire$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.holder).toBe("agent-A");
      expect(body.ttl_seconds).toBe(30);
      expect(body.wait_ms).toBe(100);
      return { body: makeLock({ name: "kv:sources/index" }) };
    });

    const { ws } = await bootstrap(server);
    const lock = await ws.locks.acquire("kv:sources/index", {
      holder: "agent-A",
      ttlSeconds: 30,
      waitMs: 100,
    });
    expect(lock.name).toBe("kv:sources/index");
    expect(lock.holder).toBe("agent-A");
  });

  it("acquire surfaces LockConflictError on 409 LOCK_HELD", async () => {
    const server = new MockServer();
    server.on("POST", /\/locks\/foo\/acquire$/, () => ({
      status: 409,
      body: {
        error: {
          code: "LOCK_HELD",
          message: "lock is currently held",
          details: {
            current_holder: "agent-A",
            retry_after_seconds: 5,
            name: "foo",
          },
        },
      },
    }));

    const { ws } = await bootstrap(server);
    await expect(
      ws.locks.acquire("foo", { holder: "agent-B", ttlSeconds: 60 }),
    ).rejects.toMatchObject({
      name: "LockConflictError",
      currentHolder: "agent-A",
      retryAfterSeconds: 5,
    });
  });

  it("heartbeat round-trips with optional ttl_seconds", async () => {
    const server = new MockServer();
    server.on("POST", /\/locks\/hb\/heartbeat$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.holder).toBe("A");
      expect(body.ttl_seconds).toBe(120);
      return { body: makeLock({ name: "hb", holder: "A" }) };
    });

    const { ws } = await bootstrap(server);
    const lock = await ws.locks.heartbeat("hb", {
      holder: "A",
      ttlSeconds: 120,
    });
    expect(lock.holder).toBe("A");
  });

  it("heartbeat 404 raises LockNotFoundError", async () => {
    const server = new MockServer();
    server.on("POST", /\/locks\/missing\/heartbeat$/, () => ({
      status: 404,
      body: {
        error: { code: "LOCK_NOT_FOUND", message: "lock not found", details: {} },
      },
    }));

    const { ws } = await bootstrap(server);
    await expect(
      ws.locks.heartbeat("missing", { holder: "A" }),
    ).rejects.toBeInstanceOf(LockNotFoundError);
  });

  it("heartbeat 409 LOCK_NOT_HELD raises LockNotHeldError", async () => {
    const server = new MockServer();
    server.on("POST", /\/locks\/wh\/heartbeat$/, () => ({
      status: 409,
      body: {
        error: { code: "LOCK_NOT_HELD", message: "wrong holder", details: {} },
      },
    }));

    const { ws } = await bootstrap(server);
    await expect(
      ws.locks.heartbeat("wh", { holder: "B" }),
    ).rejects.toBeInstanceOf(LockNotHeldError);
  });

  it("release posts holder and resolves to void", async () => {
    const server = new MockServer();
    let called = false;
    server.on("POST", /\/locks\/r\/release$/, (req) => {
      called = true;
      const body = JSON.parse(req.body ?? "{}");
      expect(body.holder).toBe("A");
      return { status: 204 };
    });

    const { ws } = await bootstrap(server);
    await ws.locks.release("r", { holder: "A" });
    expect(called).toBe(true);
  });

  it("list returns an array of Lock", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/workspaces\/ws_1\/locks$/, {
      locks: [
        makeLock({ name: "a" }),
        makeLock({ name: "b" }),
      ],
    });

    const { ws } = await bootstrap(server);
    const locks = await ws.locks.list();
    expect(locks.map((l) => l.name).sort()).toEqual(["a", "b"]);
  });

  it("get returns a single Lock or LockNotFoundError", async () => {
    const server = new MockServer();
    server.json(
      "GET",
      /\/v1\/workspaces\/ws_1\/locks\/inspect$/,
      makeLock({ name: "inspect" }),
    );
    server.on("GET", /\/v1\/workspaces\/ws_1\/locks\/missing$/, () => ({
      status: 404,
      body: { error: { code: "LOCK_NOT_FOUND", message: "not found", details: {} } },
    }));

    const { ws } = await bootstrap(server);
    const lock = await ws.locks.get("inspect");
    expect(lock.name).toBe("inspect");

    await expect(ws.locks.get("missing")).rejects.toBeInstanceOf(
      LockNotFoundError,
    );
  });

  it("withLock acquires, runs fn, and releases on resolve", async () => {
    const server = new MockServer();
    let acquired = false;
    let released = false;
    server.on("POST", /\/locks\/work\/acquire$/, () => {
      acquired = true;
      return { body: makeLock({ name: "work", holder: "A" }) };
    });
    server.on("POST", /\/locks\/work\/release$/, () => {
      released = true;
      return { status: 204 };
    });

    const { ws } = await bootstrap(server);
    const result = await ws.locks.withLock(
      "work",
      "A",
      { ttlSeconds: 30, heartbeatIntervalMs: 0 },
      async () => 42,
    );

    expect(result).toBe(42);
    expect(acquired).toBe(true);
    expect(released).toBe(true);
  });

  it("withLock releases the lock even when fn throws", async () => {
    const server = new MockServer();
    let released = false;
    server.on("POST", /\/locks\/flaky\/acquire$/, () => ({
      body: makeLock({ name: "flaky", holder: "A" }),
    }));
    server.on("POST", /\/locks\/flaky\/release$/, () => {
      released = true;
      return { status: 204 };
    });

    const { ws } = await bootstrap(server);
    await expect(
      ws.locks.withLock(
        "flaky",
        "A",
        { ttlSeconds: 30, heartbeatIntervalMs: 0 },
        async () => {
          throw new Error("boom");
        },
      ),
    ).rejects.toThrow("boom");

    expect(released).toBe(true);
  });

  it("withLock propagates LockConflictError without running fn", async () => {
    const server = new MockServer();
    server.on("POST", /\/locks\/contested\/acquire$/, () => ({
      status: 409,
      body: {
        error: {
          code: "LOCK_HELD",
          message: "held",
          details: { current_holder: "agent-X" },
        },
      },
    }));

    const { ws } = await bootstrap(server);
    let bodyRan = false;
    await expect(
      ws.locks.withLock(
        "contested",
        "me",
        { ttlSeconds: 10, heartbeatIntervalMs: 0 },
        async () => {
          bodyRan = true;
          return null;
        },
      ),
    ).rejects.toBeInstanceOf(LockConflictError);
    expect(bodyRan).toBe(false);
  });

  it("acquire URL preserves slashes in lock names", async () => {
    const server = new MockServer();
    let seenUrl = "";
    server.on("POST", /\/locks\/kv:sources\/index\/acquire$/, (req) => {
      seenUrl = req.url;
      return { body: makeLock({ name: "kv:sources/index" }) };
    });

    const { ws } = await bootstrap(server);
    await ws.locks.acquire("kv:sources/index", {
      holder: "A",
      ttlSeconds: 30,
    });
    // The slash inside the name should round-trip unescaped — that's
    // the canonical use case for the workspace's ``{name:path}`` route.
    expect(seenUrl).toContain("/locks/kv:sources/index/acquire");
  });
});
