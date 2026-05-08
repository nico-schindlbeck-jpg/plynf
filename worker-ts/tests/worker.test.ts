/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Worker tests — drive the worker through one or more poll → lease →
 * execute → release iterations using a hand-rolled fetch mock.
 *
 * The tests exercise `Worker.pollLeaseAndExecute` directly so the slot
 * loop's idle-backoff doesn't slow the suite down.
 */

import { describe, expect, it } from "vitest";

import { WorkflowRuntime, Worker, type HandlerContext } from "../src/index.js";

import {
  errorEnvelope,
  makeLease,
  makePlinth,
  makeStep,
  makeWorkflow,
  makeWorkspaceRecord,
  MockServer,
  wireWorkerRegistration,
} from "./_helpers.js";

// ---------------------------------------------------------------------------
// Wire-up helpers
// ---------------------------------------------------------------------------

interface Wired {
  server: MockServer;
  client: import("@plinth/sdk").Plinth;
  runtime: WorkflowRuntime;
  worker: Worker;
}

async function bootstrap(opts: {
  workspaceFilter?: string[];
  silent?: boolean;
} = {}): Promise<Wired> {
  const server = new MockServer();
  wireWorkerRegistration(server);
  const client = makePlinth(server);
  const runtime = new WorkflowRuntime();
  const worker = new Worker({
    client,
    runtime,
    concurrency: 1,
    leaseTtlSeconds: 30,
    heartbeatIntervalSeconds: 5,
    workerHeartbeatIntervalSeconds: 10,
    pollIntervalSeconds: 0.05,
    workspaceFilter: opts.workspaceFilter ?? null,
    logger: opts.silent !== false ? null : undefined,
  });
  // Register so workerId is set without spinning up the full run loop.
  worker.workerId = (await client.workers.register()).id;
  return { server, client, runtime, worker };
}

function wireOneWorkflow(server: MockServer, opts: {
  wsId?: string;
  workflowId?: string;
  workflowName?: string;
  steps?: unknown[];
}): void {
  const wsId = opts.wsId ?? "ws_01TEST";
  const wfId = opts.workflowId ?? "wf_01TEST";
  const wfName = opts.workflowName ?? "research";
  server.json("GET", /\/v1\/workspaces$/, {
    workspaces: [makeWorkspaceRecord({ id: wsId })],
  });
  server.json(
    "GET",
    new RegExp(`/v1/workspaces/${wsId}$`),
    makeWorkspaceRecord({ id: wsId }),
  );
  server.json(
    "GET",
    new RegExp(`/v1/workspaces/${wsId}/workflows$`),
    {
      workflows: [makeWorkflow({ id: wfId, name: wfName })],
    },
  );
  server.json(
    "GET",
    new RegExp(`/v1/workspaces/${wsId}/workflows/${wfId}$`),
    makeWorkflow({ id: wfId, name: wfName }),
  );
  server.json(
    "GET",
    new RegExp(`/v1/workspaces/${wsId}/workflows/${wfId}/pending$`),
    { steps: opts.steps ?? [makeStep({ name: "search" })] },
  );
}

// ---------------------------------------------------------------------------
// Constructor validation
// ---------------------------------------------------------------------------

describe("Worker — construction", () => {
  it("rejects heartbeat >= leaseTtl", async () => {
    const server = new MockServer();
    const client = makePlinth(server);
    const runtime = new WorkflowRuntime();
    expect(
      () =>
        new Worker({
          client,
          runtime,
          leaseTtlSeconds: 10,
          heartbeatIntervalSeconds: 10,
        }),
    ).toThrow(/heartbeat/);
    expect(
      () =>
        new Worker({
          client,
          runtime,
          leaseTtlSeconds: 10,
          heartbeatIntervalSeconds: 20,
        }),
    ).toThrow(/heartbeat/);
  });

  it("rejects concurrency < 1", () => {
    const server = new MockServer();
    const client = makePlinth(server);
    const runtime = new WorkflowRuntime();
    expect(() => new Worker({ client, runtime, concurrency: 0 })).toThrow();
  });

  it("uses sensible defaults", async () => {
    const server = new MockServer();
    const client = makePlinth(server);
    const runtime = new WorkflowRuntime();
    const worker = new Worker({ client, runtime });
    expect(worker.concurrency).toBe(4);
    expect(worker.leaseTtlSeconds).toBe(60);
    expect(worker.heartbeatIntervalSeconds).toBe(15);
    expect(worker.pollIntervalSeconds).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

describe("Worker — poll → lease → execute → release happy path", () => {
  it("dispatches the registered handler and completes the step", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, {
      steps: [
        makeStep({
          name: "search",
          input: { topic: "renewable energy" },
        }),
      ],
    });
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      makeLease(),
    );

    let releaseBody: Record<string, unknown> | null = null;
    server.on(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/release$/,
      (req) => {
        releaseBody = JSON.parse(req.body ?? "{}") as Record<string, unknown>;
        return { body: makeLease({ status: "released" }) };
      },
    );

    const calls: HandlerContext[] = [];
    runtime.register("research", "search", (ctx) => {
      calls.push(ctx);
      return { sources: ["a", "b"] };
    });

    const claimed = await worker.pollLeaseAndExecute();
    expect(claimed).toBe(true);
    expect(calls).toHaveLength(1);
    const inputs = calls[0]?.step.input as { topic: string };
    expect(inputs.topic).toBe("renewable energy");
    expect(worker.getStats().leased).toBe(1);
    expect(worker.getStats().completed).toBe(1);
    expect(worker.getStats().failed).toBe(0);
    expect(releaseBody).toMatchObject({
      status: "completed",
      output: { sources: ["a", "b"] },
    });
  });

  it("supports an async handler", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { steps: [makeStep({ name: "search" })] });
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      makeLease(),
    );
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/release$/,
      makeLease({ status: "released" }),
    );
    runtime.register("research", "search", async () => {
      await new Promise<void>((r) => setTimeout(r, 1));
      return { async: true };
    });
    expect(await worker.pollLeaseAndExecute()).toBe(true);
    expect(worker.getStats().completed).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Failure path
// ---------------------------------------------------------------------------

describe("Worker — handler failures", () => {
  it("releases with status=failed when the handler throws", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { steps: [makeStep({ name: "search" })] });
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      makeLease(),
    );

    let releaseBody: Record<string, unknown> | null = null;
    server.on(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/release$/,
      (req) => {
        releaseBody = JSON.parse(req.body ?? "{}") as Record<string, unknown>;
        return { body: makeLease({ status: "released" }) };
      },
    );

    runtime.register("research", "search", () => {
      throw new Error("synthetic boom");
    });
    expect(await worker.pollLeaseAndExecute()).toBe(true);
    expect(worker.getStats().failed).toBe(1);
    expect(worker.getStats().completed).toBe(0);
    expect(releaseBody).toMatchObject({ status: "failed" });
    expect(((releaseBody as Record<string, unknown>).error as string)).toMatch(/synthetic boom/);
  });

  it("returns false (no claim) when no pending steps are visible", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { steps: [] });
    runtime.register("research", "search", () => null);
    expect(await worker.pollLeaseAndExecute()).toBe(false);
    expect(worker.getStats()).toMatchObject({ leased: 0, completed: 0, failed: 0, lost: 0 });
  });

  it("skips workflows with no matching handler", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { workflowName: "other" });
    runtime.register("research", "search", () => null);
    expect(await worker.pollLeaseAndExecute()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Lease conflict (someone else won)
// ---------------------------------------------------------------------------

describe("Worker — concurrency", () => {
  it("counts a 409 LEASE_CONFLICT as a lost claim", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { steps: [makeStep({ name: "search" })] });
    server.on(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      () => ({
        status: 409,
        body: errorEnvelope("LEASE_CONFLICT", "another worker beat us"),
      }),
    );
    runtime.register("research", "search", () => null);
    expect(await worker.pollLeaseAndExecute()).toBe(false);
    expect(worker.getStats().lost).toBe(1);
    expect(worker.getStats().leased).toBe(0);
  });

  it("two workers contending: exactly one wins", async () => {
    // One MockServer; two clients sharing it. The lease endpoint flips
    // 200 → 409 on consecutive calls, so the first caller wins.
    const server = new MockServer();
    wireWorkerRegistration(server);
    wireOneWorkflow(server, { steps: [makeStep({ name: "search" })] });

    let leaseN = 0;
    server.on(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      () => {
        leaseN += 1;
        if (leaseN === 1) return { body: makeLease() };
        return {
          status: 409,
          body: errorEnvelope("LEASE_CONFLICT", "lost the race"),
        };
      },
    );
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/release$/,
      makeLease({ status: "released" }),
    );

    const a = makePlinth(server);
    const b = makePlinth(server);
    const rtA = new WorkflowRuntime();
    const rtB = new WorkflowRuntime();
    rtA.register("research", "search", () => ({ who: "a" }));
    rtB.register("research", "search", () => ({ who: "b" }));
    const wa = new Worker({
      client: a,
      runtime: rtA,
      concurrency: 1,
      leaseTtlSeconds: 30,
      heartbeatIntervalSeconds: 5,
      logger: null,
    });
    const wb = new Worker({
      client: b,
      runtime: rtB,
      concurrency: 1,
      leaseTtlSeconds: 30,
      heartbeatIntervalSeconds: 5,
      logger: null,
    });
    wa.workerId = (await a.workers.register()).id;
    wb.workerId = (await b.workers.register()).id;

    const aClaim = await wa.pollLeaseAndExecute();
    const bClaim = await wb.pollLeaseAndExecute();

    expect([aClaim, bClaim].sort()).toEqual([false, true]);
    expect(wa.getStats().completed + wb.getStats().completed).toBe(1);
    expect(wa.getStats().lost + wb.getStats().lost).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Workspace filter
// ---------------------------------------------------------------------------

describe("Worker — workspace filter", () => {
  it("ignores workspaces outside the filter", async () => {
    const { server, runtime, worker } = await bootstrap({
      workspaceFilter: ["ws_a"],
    });
    server.json("GET", /\/v1\/workspaces$/, {
      workspaces: [
        makeWorkspaceRecord({ id: "ws_a", name: "alpha" }),
        makeWorkspaceRecord({ id: "ws_b", name: "beta" }),
      ],
    });
    server.json(
      "GET",
      /\/v1\/workspaces\/ws_a$/,
      makeWorkspaceRecord({ id: "ws_a", name: "alpha" }),
    );
    server.json("GET", /\/v1\/workspaces\/ws_a\/workflows$/, { workflows: [] });
    runtime.register("research", "search", () => null);
    expect(await worker.pollLeaseAndExecute()).toBe(false);

    // No request to ws_b should have been issued.
    const reachedWsB = server.requests.find((r) => r.url.includes("/workspaces/ws_b"));
    expect(reachedWsB).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// run() + stop() lifecycle
// ---------------------------------------------------------------------------

describe("Worker — lifecycle", () => {
  it("registers on run() and drains on stop()", async () => {
    const server = new MockServer();
    wireWorkerRegistration(server);
    server.json("GET", /\/v1\/workspaces$/, { workspaces: [] });

    const client = makePlinth(server);
    const runtime = new WorkflowRuntime();
    runtime.register("never", "matched", () => null);
    const worker = new Worker({
      client,
      runtime,
      concurrency: 1,
      leaseTtlSeconds: 30,
      heartbeatIntervalSeconds: 5,
      pollIntervalSeconds: 0.01,
      logger: null,
    });

    const runP = worker.run();
    // Give the slot loop one tick to register and start polling.
    await new Promise<void>((r) => setTimeout(r, 50));
    expect(worker.workerId).toBe("worker_01TEST");

    await worker.stop();
    await runP;

    const drained = server.requests.find((r) =>
      r.url.includes("/v1/workers/worker_01TEST/drain"),
    );
    expect(drained).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Output coercion
// ---------------------------------------------------------------------------

describe("Worker — output coercion", () => {
  it("normalises an undefined return to null in the release body", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { steps: [makeStep({ name: "search" })] });
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      makeLease(),
    );
    let releaseBody: Record<string, unknown> | null = null;
    server.on(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/release$/,
      (req) => {
        releaseBody = JSON.parse(req.body ?? "{}") as Record<string, unknown>;
        return { body: makeLease({ status: "released" }) };
      },
    );
    runtime.register("research", "search", () => undefined);
    expect(await worker.pollLeaseAndExecute()).toBe(true);
    // `output: null` is included on the wire so the workspace stores
    // an explicit null, distinguishable from "field absent".
    expect(releaseBody).toMatchObject({ status: "completed", output: null });
  });

  it("serialises a structured handler return into the release body", async () => {
    const { server, runtime, worker } = await bootstrap();
    wireOneWorkflow(server, { steps: [makeStep({ name: "search" })] });
    server.json(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/lease$/,
      makeLease(),
    );
    let releaseBody: Record<string, unknown> | null = null;
    server.on(
      "POST",
      /\/v1\/workspaces\/ws_01TEST\/workflows\/wf_01TEST\/steps\/step_01TEST\/release$/,
      (req) => {
        releaseBody = JSON.parse(req.body ?? "{}") as Record<string, unknown>;
        return { body: makeLease({ status: "released" }) };
      },
    );
    runtime.register("research", "search", () => ({
      sources_count: 5,
      snapshot_id: "snap_1",
    }));
    expect(await worker.pollLeaseAndExecute()).toBe(true);
    expect((releaseBody as Record<string, unknown>).output).toEqual({
      sources_count: 5,
      snapshot_id: "snap_1",
    });
  });
});

// ---------------------------------------------------------------------------
// CLI parsing
// ---------------------------------------------------------------------------

describe("CLI — argument parsing", () => {
  it("layers CLI flags on top of defaults", async () => {
    const { parseCli } = await import("../src/cli.js");
    const args = parseCli([
      "--workspace-url",
      "http://ws.example",
      "--api-key",
      "abc",
      "--concurrency",
      "8",
      "--lease-ttl",
      "120",
      "--heartbeat-interval",
      "30",
      "--handlers-module",
      "./handlers.js",
    ]);
    expect(args.workspaceUrl).toBe("http://ws.example");
    expect(args.apiKey).toBe("abc");
    expect(args.concurrency).toBe(8);
    expect(args.leaseTtl).toBe(120);
    expect(args.heartbeatInterval).toBe(30);
    expect(args.handlersModule).toBe("./handlers.js");
  });

  it("reads from PLINTH_* env vars when flags are absent", async () => {
    const { parseCli } = await import("../src/cli.js");
    const before = process.env.PLINTH_WORKSPACE_URL;
    process.env.PLINTH_WORKSPACE_URL = "http://env.example";
    try {
      const args = parseCli(["--handlers-module", "./h.js"]);
      expect(args.workspaceUrl).toBe("http://env.example");
    } finally {
      if (before === undefined) delete process.env.PLINTH_WORKSPACE_URL;
      else process.env.PLINTH_WORKSPACE_URL = before;
    }
  });

  it("rejects non-numeric concurrency", async () => {
    const { parseCli } = await import("../src/cli.js");
    expect(() => parseCli(["--concurrency", "huh", "--handlers-module", "h"])).toThrow();
  });
});
