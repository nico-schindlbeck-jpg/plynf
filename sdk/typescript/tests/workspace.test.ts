/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import {
  KeyNotFoundError,
  Plinth,
  type FileEntry,
  type KVEntry,
  type Snapshot,
  type Workspace,
} from "../src/index.js";
import { MockServer, parseQuery } from "./_helpers.js";

async function bootstrap(server: MockServer): Promise<{ client: Plinth; ws: Workspace }> {
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
  return { client, ws };
}

describe("Workspace KV", () => {
  it("PUTs new versions and reads them back", async () => {
    const server = new MockServer();
    server.on("PUT", /\/kv\/topic$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      const entry: KVEntry = {
        workspace_id: "ws_1",
        key: "topic",
        value: body.value,
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        deleted: false,
        branch_id: null,
      };
      return { body: entry };
    });
    server.on("GET", /\/kv\/topic(\?.*)?$/, () => ({
      body: {
        workspace_id: "ws_1",
        key: "topic",
        value: "renewable energy",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        deleted: false,
        branch_id: null,
      },
    }));

    const { ws } = await bootstrap(server);
    const written = await ws.kv.set("topic", "renewable energy");
    expect(written.version).toBe(1);
    expect(written.value).toBe("renewable energy");

    const value = await ws.kv.get("topic");
    expect(value).toBe("renewable energy");

    const meta = await ws.kv.getWithMeta("topic");
    expect(meta.version).toBe(1);
  });

  it("returns null on a tombstoned key", async () => {
    const server = new MockServer();
    server.on("GET", /\/kv\/dead/, () => ({
      body: {
        workspace_id: "ws_1",
        key: "dead",
        value: null,
        version: 2,
        created_at: "2026-01-01T00:00:00Z",
        deleted: true,
        branch_id: null,
      },
    }));

    const { ws } = await bootstrap(server);
    expect(await ws.kv.get("dead")).toBeNull();
  });

  it("history returns all versions", async () => {
    const server = new MockServer();
    server.json("GET", /\/kv\/topic\/history/, {
      versions: [
        { workspace_id: "ws_1", key: "topic", value: "a", version: 1, created_at: "2026-01-01T00:00:00Z", deleted: false, branch_id: null },
        { workspace_id: "ws_1", key: "topic", value: "b", version: 2, created_at: "2026-01-01T00:00:01Z", deleted: false, branch_id: null },
      ],
    });
    const { ws } = await bootstrap(server);
    const history = await ws.kv.history("topic");
    expect(history).toHaveLength(2);
    expect(history[1]!.value).toBe("b");
  });

  it("propagates KeyNotFoundError", async () => {
    const server = new MockServer();
    server.on("GET", /\/kv\/missing/, () => ({
      status: 404,
      body: { error: { code: "KEY_NOT_FOUND", message: "no such key" } },
    }));
    const { ws } = await bootstrap(server);
    await expect(ws.kv.get("missing")).rejects.toBeInstanceOf(KeyNotFoundError);
  });

  it("DELETE issues a tombstone", async () => {
    const server = new MockServer();
    server.on("DELETE", /\/kv\/dead/, () => ({ status: 204 }));
    const { ws } = await bootstrap(server);
    await expect(ws.kv.delete("dead")).resolves.toBeUndefined();
    expect(server.requests.find((r) => r.method === "DELETE")).toBeDefined();
  });

  it("encodes keys with special characters", async () => {
    const server = new MockServer();
    server.json("PUT", /\/kv\/key%20with%20space/, {
      workspace_id: "ws_1",
      key: "key with space",
      value: 1,
      version: 1,
      created_at: "2026-01-01T00:00:00Z",
      deleted: false,
      branch_id: null,
    });
    const { ws } = await bootstrap(server);
    await ws.kv.set("key with space", 1);
    expect(server.requests.some((r) => r.url.includes("key%20with%20space"))).toBe(true);
  });
});

describe("Workspace Files", () => {
  it("writes string content as bytes and reads back text", async () => {
    const server = new MockServer();
    server.on("PUT", /\/files\/report\.md/, () => ({
      body: {
        workspace_id: "ws_1",
        path: "report.md",
        size: 13,
        sha256: "deadbeef",
        content_type: "text/plain; charset=utf-8",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        deleted: false,
        branch_id: null,
      } as FileEntry,
    }));
    server.on("GET", /\/files\/report\.md/, (req) => {
      // Don't match /meta route
      if (req.url.endsWith("/meta")) return { status: 404, body: { error: { code: "FILE_NOT_FOUND", message: "" } } };
      return { bodyBytes: new TextEncoder().encode("# Report\n...") };
    });

    const { ws } = await bootstrap(server);
    const meta = await ws.files.write("report.md", "# Report\n...");
    expect(meta.size).toBe(13);

    const text = await ws.files.readText("report.md");
    expect(text).toBe("# Report\n...");
  });

  it("preserves nested file paths", async () => {
    const server = new MockServer();
    server.on("PUT", /\/files\/notes\/2026\/q1\.md/, () => ({
      body: {
        workspace_id: "ws_1",
        path: "notes/2026/q1.md",
        size: 5,
        sha256: "x",
        content_type: "text/plain; charset=utf-8",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        deleted: false,
        branch_id: null,
      } as FileEntry,
    }));
    const { ws } = await bootstrap(server);
    await ws.files.write("notes/2026/q1.md", "hello");
    const matched = server.requests.find((r) => r.url.includes("/files/notes/2026/q1.md"));
    expect(matched).toBeDefined();
  });
});

describe("Workspace snapshots and branches", () => {
  it("creates a snapshot with optional message", async () => {
    const server = new MockServer();
    server.on("POST", /\/snapshots$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      const snap: Snapshot = {
        id: "snap_1",
        workspace_id: "ws_1",
        name: body.name,
        message: body.message ?? null,
        created_at: "2026-01-01T00:00:00Z",
        kv_versions: {},
        file_versions: {},
        parent_snapshot_id: null,
      };
      return { status: 201, body: snap };
    });
    const { ws } = await bootstrap(server);
    const snap = await ws.snapshot("baseline", { message: "initial state" });
    expect(snap.id).toBe("snap_1");
    expect(snap.message).toBe("initial state");
  });

  it("withBranch attaches ?branch= to KV reads/writes", async () => {
    const server = new MockServer();
    server.on("PUT", /\/kv\/topic/, (req) => {
      const q = parseQuery(req.url);
      expect(q.branch).toBe("br_1");
      return {
        body: {
          workspace_id: "ws_1",
          key: "topic",
          value: JSON.parse(req.body ?? "{}").value,
          version: 2,
          created_at: "2026-01-01T00:00:00Z",
          deleted: false,
          branch_id: "br_1",
        } as KVEntry,
      };
    });

    const { ws } = await bootstrap(server);
    const wsB = ws.withBranch("br_1");
    const written = await wsB.kv.set("topic", "branch value");
    expect(written.branch_id).toBe("br_1");
    // The original ws should still be branch-less
    expect(ws.branchId).toBeNull();
  });

  it("merge POSTs to the branch merge endpoint", async () => {
    const server = new MockServer();
    server.on("POST", /\/branches\/br_1\/merge/, () => ({
      body: { branch_id: "br_1", merged: true, merged_at: "2026-01-01T00:00:01Z", conflicts: [] },
    }));
    const { ws } = await bootstrap(server);
    const result = await ws.merge("br_1");
    expect(result.merged).toBe(true);
  });
});
