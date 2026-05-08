/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import {
  ChannelNotFoundError,
  MessageNotFoundError,
  Plinth,
  SchemaViolationError,
  type Channel,
  type ChannelMessage,
  type ChannelSchema,
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

function fakeMessage(seq: number, partial: Partial<ChannelMessage> = {}): ChannelMessage {
  return {
    id: partial.id ?? `msg_${seq}`,
    channel: partial.channel ?? "research-out",
    workspace_id: "ws_1",
    seq,
    payload: partial.payload ?? { hello: "world" },
    sender: partial.sender ?? null,
    type: partial.type ?? null,
    correlation_id: partial.correlation_id ?? null,
    headers: partial.headers ?? {},
    sent_at: partial.sent_at ?? "2026-01-01T00:00:00Z",
    delivered_at: partial.delivered_at ?? null,
  };
}

describe("ChannelsClient — send / receive / ack", () => {
  it("send POSTs payload + optional metadata and returns the persisted message", async () => {
    const server = new MockServer();
    server.on("POST", /\/channels\/research-out\/send$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.payload).toEqual({ topic: "renewables" });
      expect(body.sender).toBe("researcher");
      expect(body.type).toBe("research.complete");
      expect(body.correlation_id).toBe("corr-1");
      expect(body.headers).toEqual({ "x-key": "v" });
      return {
        status: 201,
        body: fakeMessage(1, {
          payload: body.payload,
          sender: body.sender,
          type: body.type,
          correlation_id: body.correlation_id,
          headers: body.headers,
        }),
      };
    });

    const { ws } = await bootstrap(server);
    const msg = await ws.channels.send(
      "research-out",
      { topic: "renewables" },
      {
        sender: "researcher",
        type: "research.complete",
        correlationId: "corr-1",
        headers: { "x-key": "v" },
      },
    );
    expect(msg.id).toBe("msg_1");
    expect(msg.seq).toBe(1);
    expect(msg.sender).toBe("researcher");
  });

  it("send with bare payload omits optional fields from the body", async () => {
    const server = new MockServer();
    let captured: Record<string, unknown> = {};
    server.on("POST", /\/channels\/c1\/send$/, (req) => {
      captured = JSON.parse(req.body ?? "{}");
      return { status: 201, body: fakeMessage(1, { channel: "c1" }) };
    });
    const { ws } = await bootstrap(server);
    await ws.channels.send("c1", "ping");
    expect(captured).toEqual({ payload: "ping" });
  });

  it("receive returns the messages array and forwards consumer/limit/peek", async () => {
    const server = new MockServer();
    server.on("GET", /\/channels\/c1\/receive/, (req) => {
      const q = parseQuery(req.url);
      expect(q.consumer).toBe("writer");
      expect(q.limit).toBe("10");
      expect(q.peek).toBe("true");
      return {
        body: {
          messages: [
            fakeMessage(1, { channel: "c1" }),
            fakeMessage(2, { channel: "c1" }),
          ],
        },
      };
    });
    const { ws } = await bootstrap(server);
    const msgs = await ws.channels.receive("c1", { consumer: "writer", limit: 10, peek: true });
    expect(msgs).toHaveLength(2);
    expect(msgs[0]!.payload).toEqual({ hello: "world" });
  });

  it("receive on an empty channel returns []", async () => {
    const server = new MockServer();
    server.json("GET", /\/channels\/c1\/receive/, { messages: [] });
    const { ws } = await bootstrap(server);
    const msgs = await ws.channels.receive("c1");
    expect(msgs).toEqual([]);
  });

  it("ack issues DELETE on the message route and returns void", async () => {
    const server = new MockServer();
    server.on("DELETE", /\/channels\/c1\/messages\/msg_1$/, () => ({ status: 204 }));
    const { ws } = await bootstrap(server);
    const msg = fakeMessage(1, { channel: "c1", id: "msg_1" });
    await expect(ws.channels.ack(msg)).resolves.toBeUndefined();
    expect(server.requests.find((r) => r.method === "DELETE")).toBeDefined();
  });

  it("delete is an alias for ack", async () => {
    const server = new MockServer();
    let saw = 0;
    server.on("DELETE", /\/channels\/c1\/messages\/msg_2$/, () => {
      saw += 1;
      return { status: 204 };
    });
    const { ws } = await bootstrap(server);
    const msg = fakeMessage(2, { channel: "c1", id: "msg_2" });
    await ws.channels.delete(msg);
    expect(saw).toBe(1);
  });

  it("ack rejects bare-string IDs with a TypeError", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);
    // Cast through unknown so we exercise the runtime guard.
    await expect(
      ws.channels.ack("msg_1" as unknown as ChannelMessage),
    ).rejects.toBeInstanceOf(TypeError);
  });
});

describe("ChannelsClient — wait", () => {
  it("returns null on timeout when the channel stays empty", async () => {
    const server = new MockServer();
    server.json("GET", /\/channels\/empty\/receive/, { messages: [] });

    const { ws } = await bootstrap(server);
    const result = await ws.channels.wait("empty", { timeoutMs: 50, pollIntervalMs: 10 });
    expect(result).toBeNull();
    // We expect at least one poll.
    expect(server.requests.filter((r) => r.method === "GET" && r.url.includes("/channels/empty/receive")).length).toBeGreaterThan(0);
  });

  it("returns the first message once one shows up", async () => {
    const server = new MockServer();
    let polls = 0;
    server.on("GET", /\/channels\/p\/receive/, () => {
      polls += 1;
      if (polls < 3) return { body: { messages: [] } };
      return { body: { messages: [fakeMessage(1, { channel: "p" })] } };
    });
    const { ws } = await bootstrap(server);
    const msg = await ws.channels.wait("p", { timeoutMs: 5_000, pollIntervalMs: 5 });
    expect(msg?.id).toBe("msg_1");
    expect(polls).toBeGreaterThanOrEqual(3);
  });
});

describe("ChannelsClient — channel management", () => {
  it("list returns all channels", async () => {
    const server = new MockServer();
    server.json("GET", /\/channels$/, {
      channels: [
        {
          name: "research-out",
          workspace_id: "ws_1",
          message_count: 3,
          created_at: "2026-01-01T00:00:00Z",
          last_send_at: "2026-01-01T00:00:01Z",
          last_receive_at: null,
        } satisfies Channel,
      ],
    });
    const { ws } = await bootstrap(server);
    const channels = await ws.channels.list();
    expect(channels).toHaveLength(1);
    expect(channels[0]!.name).toBe("research-out");
  });

  it("get returns a single channel", async () => {
    const server = new MockServer();
    server.json("GET", /\/channels\/research-out$/, {
      name: "research-out",
      workspace_id: "ws_1",
      message_count: 0,
      created_at: "2026-01-01T00:00:00Z",
      last_send_at: null,
      last_receive_at: null,
    } satisfies Channel);
    const { ws } = await bootstrap(server);
    const ch = await ws.channels.get("research-out");
    expect(ch.name).toBe("research-out");
  });

  it("get maps CHANNEL_NOT_FOUND to a typed error", async () => {
    const server = new MockServer();
    server.on("GET", /\/channels\/nope$/, () => ({
      status: 404,
      body: { error: { code: "CHANNEL_NOT_FOUND", message: "no such channel" } },
    }));
    const { ws } = await bootstrap(server);
    await expect(ws.channels.get("nope")).rejects.toBeInstanceOf(ChannelNotFoundError);
  });

  it("deleteChannel issues DELETE on the channel route", async () => {
    const server = new MockServer();
    server.on("DELETE", /\/channels\/research-out$/, () => ({ status: 204 }));
    const { ws } = await bootstrap(server);
    await expect(ws.channels.deleteChannel("research-out")).resolves.toBeUndefined();
  });

  it("propagates ?branch= when the workspace is branch-scoped", async () => {
    const server = new MockServer();
    server.on("POST", /\/channels\/c\/send/, (req) => {
      expect(parseQuery(req.url).branch).toBe("br_1");
      return { status: 201, body: fakeMessage(1, { channel: "c" }) };
    });
    const { ws } = await bootstrap(server);
    await ws.withBranch("br_1").channels.send("c", { x: 1 });
  });
});


// ---------------------------------------------------------------------------
// v0.5 — typed channels + dead-letter queue
// ---------------------------------------------------------------------------

const SIMPLE_SCHEMA = {
  type: "object",
  required: ["topic", "sources"],
  properties: {
    topic: { type: "string" },
    sources: { type: "array", items: { type: "string" } },
  },
};

function fakeSchema(version = 1): ChannelSchema {
  return {
    workspace_id: "ws_1",
    channel_name: "research-out",
    schema_json: SIMPLE_SCHEMA,
    version,
    updated_at: "2026-05-07T16:30:00Z",
  };
}

describe("ChannelsClient — schema CRUD + DLQ", () => {
  it("setSchema POSTs {schema: ...} and returns ChannelSchema", async () => {
    const server = new MockServer();
    server.on("POST", /\/channels\/research-out\/schema$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body).toEqual({ schema: SIMPLE_SCHEMA });
      return { status: 200, body: fakeSchema(1) };
    });
    const { ws } = await bootstrap(server);
    const result = await ws.channels.setSchema("research-out", SIMPLE_SCHEMA);
    expect(result.version).toBe(1);
    expect(result.channel_name).toBe("research-out");
  });

  it("getSchema returns the persisted schema", async () => {
    const server = new MockServer();
    server.json("GET", /\/channels\/research-out\/schema$/, fakeSchema(2));
    const { ws } = await bootstrap(server);
    const result = await ws.channels.getSchema("research-out");
    expect(result?.version).toBe(2);
    expect(result?.schema_json).toEqual(SIMPLE_SCHEMA);
  });

  it("getSchema returns null on 404", async () => {
    const server = new MockServer();
    server.on("GET", /\/channels\/no-schema\/schema$/, () => ({
      status: 404,
      body: { error: { code: "SCHEMA_NOT_FOUND", message: "no schema" } },
    }));
    const { ws } = await bootstrap(server);
    const result = await ws.channels.getSchema("no-schema");
    expect(result).toBeNull();
  });

  it("deleteSchema DELETEs the schema route", async () => {
    const server = new MockServer();
    let called = false;
    server.on("DELETE", /\/channels\/research-out\/schema$/, () => {
      called = true;
      return { status: 204 };
    });
    const { ws } = await bootstrap(server);
    await expect(ws.channels.deleteSchema("research-out")).resolves.toBeUndefined();
    expect(called).toBe(true);
  });

  it("send throws SchemaViolationError carrying errors + deadletterMsgId", async () => {
    const server = new MockServer();
    server.on("POST", /\/channels\/research-out\/send$/, () => ({
      status: 422,
      body: {
        error: {
          code: "SCHEMA_VIOLATION",
          message: "Payload does not match channel schema",
          details: {
            channel: "research-out",
            errors: [
              {
                message: "'sources' is a required property",
                path: [],
                validator: "required",
              },
            ],
            deadletter_msg_id: "msg_dlq_01",
          },
        },
      },
    }));
    const { ws } = await bootstrap(server);
    let caught: unknown;
    try {
      await ws.channels.send("research-out", { topic: "ai" });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(SchemaViolationError);
    const err = caught as SchemaViolationError;
    expect(err.code).toBe("SCHEMA_VIOLATION");
    expect(err.deadletterMsgId).toBe("msg_dlq_01");
    expect(err.channel).toBe("research-out");
    expect(err.errors).toHaveLength(1);
    expect(err.errors[0]!.message).toContain("sources");
  });

  it("deadletter lists DLQ messages", async () => {
    const server = new MockServer();
    server.on("GET", /\/channels\/research-out\/deadletter/, (req) => {
      const q = parseQuery(req.url);
      expect(q.limit).toBe("50");
      expect(q.since).toBe("3");
      return {
        status: 200,
        body: {
          messages: [
            fakeMessage(4, {
              id: "msg_dlq_01",
              channel: "research-out.deadletter",
              headers: { "x-original-channel": "research-out" },
            }),
            fakeMessage(5, {
              id: "msg_dlq_02",
              channel: "research-out.deadletter",
              headers: { "x-original-channel": "research-out" },
            }),
          ],
        },
      };
    });
    const { ws } = await bootstrap(server);
    const msgs = await ws.channels.deadletter("research-out", { limit: 50, since: 3 });
    expect(msgs).toHaveLength(2);
    expect(msgs[0]!.id).toBe("msg_dlq_01");
  });

  it("replay returns the freshly-sent main-channel message", async () => {
    const server = new MockServer();
    server.on(
      "POST",
      /\/channels\/research-out\/deadletter\/msg_dlq_01\/replay$/,
      () => ({
        status: 200,
        body: fakeMessage(1, { id: "msg_new", channel: "research-out" }),
      }),
    );
    const { ws } = await bootstrap(server);
    const msg = await ws.channels.replay("research-out", "msg_dlq_01");
    expect(msg.id).toBe("msg_new");
    expect(msg.channel).toBe("research-out");
  });

  it("replay accepts a ChannelMessage object and uses its id", async () => {
    const server = new MockServer();
    let called = false;
    server.on(
      "POST",
      /\/channels\/research-out\/deadletter\/msg_dlq_obj\/replay$/,
      () => {
        called = true;
        return {
          status: 200,
          body: fakeMessage(1, { id: "msg_new", channel: "research-out" }),
        };
      },
    );
    const { ws } = await bootstrap(server);
    const dlq = fakeMessage(7, {
      id: "msg_dlq_obj",
      channel: "research-out.deadletter",
    });
    await ws.channels.replay("research-out", dlq);
    expect(called).toBe(true);
  });

  it("replay throws SchemaViolationError on still-invalid", async () => {
    const server = new MockServer();
    server.on(
      "POST",
      /\/channels\/research-out\/deadletter\/msg_dlq_01\/replay$/,
      () => ({
        status: 422,
        body: {
          error: {
            code: "SCHEMA_VIOLATION",
            message: "still invalid",
            details: {
              channel: "research-out",
              errors: [{ message: "boom", path: [] }],
              deadletter_msg_id: "msg_dlq_01",
            },
          },
        },
      }),
    );
    const { ws } = await bootstrap(server);
    await expect(
      ws.channels.replay("research-out", "msg_dlq_01"),
    ).rejects.toBeInstanceOf(SchemaViolationError);
  });

  it("dropDeadletter DELETEs the message", async () => {
    const server = new MockServer();
    let called = false;
    server.on(
      "DELETE",
      /\/channels\/research-out\/deadletter\/msg_dlq_01$/,
      () => {
        called = true;
        return { status: 204 };
      },
    );
    const { ws } = await bootstrap(server);
    await ws.channels.dropDeadletter("research-out", "msg_dlq_01");
    expect(called).toBe(true);
  });

  it("dropDeadletter 404 → MessageNotFoundError", async () => {
    const server = new MockServer();
    server.on(
      "DELETE",
      /\/channels\/research-out\/deadletter\/msg_unknown$/,
      () => ({
        status: 404,
        body: { error: { code: "MESSAGE_NOT_FOUND", message: "no" } },
      }),
    );
    const { ws } = await bootstrap(server);
    await expect(
      ws.channels.dropDeadletter("research-out", "msg_unknown"),
    ).rejects.toBeInstanceOf(MessageNotFoundError);
  });
});


// ---------------------------------------------------------------------------
// v0.6 — channel schema migration helpers
// ---------------------------------------------------------------------------


describe("ChannelsClient — v0.6 schema migration helpers", () => {
  it("checkSchema POSTs schema/scope/limit and parses the result", async () => {
    const server = new MockServer();
    server.on("POST", /\/channels\/research-out\/schema\/check$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.schema).toEqual(SIMPLE_SCHEMA);
      expect(body.scope).toBe("both");
      expect(body.limit).toBe(500);
      return {
        status: 200,
        body: {
          channel: "research-out",
          scope: "both",
          checked: 7,
          valid: 5,
          invalid: 2,
          sample_failures: [
            { msg_id: "msg_d1", errors: [{ path: [], message: "boom" }] },
          ],
        },
      };
    });

    const { ws } = await bootstrap(server);
    const result = await ws.channels.checkSchema(
      "research-out",
      SIMPLE_SCHEMA,
      { scope: "both", limit: 500 },
    );
    expect(result.checked).toBe(7);
    expect(result.invalid).toBe(2);
    expect(result.sample_failures).toHaveLength(1);
    expect(result.sample_failures[0]!.msg_id).toBe("msg_d1");
  });

  it("checkSchema defaults scope='both' and limit=1000", async () => {
    const server = new MockServer();
    let capturedBody: Record<string, unknown> = {};
    server.on("POST", /\/channels\/c\/schema\/check$/, (req) => {
      capturedBody = JSON.parse(req.body ?? "{}");
      return {
        status: 200,
        body: {
          channel: "c",
          scope: "both",
          checked: 0,
          valid: 0,
          invalid: 0,
          sample_failures: [],
        },
      };
    });
    const { ws } = await bootstrap(server);
    await ws.channels.checkSchema("c", { type: "object" });
    expect(capturedBody.scope).toBe("both");
    expect(capturedBody.limit).toBe(1000);
  });

  it("replayAllDlq forwards max + dryRun=true as query params", async () => {
    const server = new MockServer();
    server.on(
      "POST",
      /\/channels\/c\/deadletter\/replay-all/,
      (req) => {
        const q = parseQuery(req.url);
        expect(q.max).toBe("50");
        expect(q.dry_run).toBe("true");
        return {
          status: 200,
          body: {
            channel: "c",
            attempted: 3,
            succeeded: 3,
            failed: 0,
            failures: [],
            dry_run: true,
          },
        };
      },
    );
    const { ws } = await bootstrap(server);
    const result = await ws.channels.replayAllDlq("c", { max: 50, dryRun: true });
    expect(result.dry_run).toBe(true);
    expect(result.attempted).toBe(3);
  });

  it("replayAllDlq omits dry_run when false; defaults max=1000", async () => {
    const server = new MockServer();
    server.on(
      "POST",
      /\/channels\/c\/deadletter\/replay-all/,
      (req) => {
        const q = parseQuery(req.url);
        expect(q.max).toBe("1000");
        expect(q.dry_run).toBeUndefined();
        return {
          status: 200,
          body: {
            channel: "c",
            attempted: 5,
            succeeded: 4,
            failed: 1,
            failures: [{ msg_id: "msg_x", reason: "still bad" }],
            dry_run: false,
          },
        };
      },
    );
    const { ws } = await bootstrap(server);
    const result = await ws.channels.replayAllDlq("c");
    expect(result.failed).toBe(1);
    expect(result.failures[0]!.reason).toBe("still bad");
  });

  it("purgeDlq returns the integer count from the response", async () => {
    const server = new MockServer();
    server.on("DELETE", /\/channels\/c\/deadletter/, (req) => {
      const q = parseQuery(req.url);
      expect(q.older_than_seconds).toBe("86400");
      return { status: 200, body: { purged: 7 } };
    });
    const { ws } = await bootstrap(server);
    const count = await ws.channels.purgeDlq("c", { olderThanSeconds: 86400 });
    expect(count).toBe(7);
  });

  it("purgeDlq defaults olderThanSeconds=0 (purge-all)", async () => {
    const server = new MockServer();
    server.on("DELETE", /\/channels\/c\/deadletter/, (req) => {
      const q = parseQuery(req.url);
      expect(q.older_than_seconds).toBe("0");
      return { status: 200, body: { purged: 3 } };
    });
    const { ws } = await bootstrap(server);
    const count = await ws.channels.purgeDlq("c");
    expect(count).toBe(3);
  });

  it("checkSchema URL-encodes the channel name", async () => {
    const server = new MockServer();
    let saw = false;
    server.on("POST", /\/channels\/with%20space\/schema\/check$/, () => {
      saw = true;
      return {
        status: 200,
        body: {
          channel: "with space",
          scope: "both",
          checked: 0,
          valid: 0,
          invalid: 0,
          sample_failures: [],
        },
      };
    });
    const { ws } = await bootstrap(server);
    await ws.channels.checkSchema("with space", { type: "object" });
    expect(saw).toBe(true);
  });
});
