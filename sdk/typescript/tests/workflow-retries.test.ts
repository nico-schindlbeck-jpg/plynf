/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * SDK-level tests for v1.1 workflow retries + DLQ.
 */

import { describe, expect, it } from "vitest";

import {
  Plinth,
  type DLQEntry,
  type DLQReplayResult,
  type Workflow,
  type WorkflowStep,
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

function fakeWorkflow(partial: Partial<Workflow> = {}): Workflow {
  return {
    id: partial.id ?? "wf_1",
    workspace_id: "ws_1",
    name: partial.name ?? "pipeline",
    steps_manifest: partial.steps_manifest ?? ["search", "fetch"],
    steps: partial.steps ?? [],
    status: partial.status ?? "pending",
    metadata: partial.metadata ?? {},
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
  };
}

function fakeStep(partial: Partial<WorkflowStep> = {}): WorkflowStep {
  return {
    id: partial.id ?? "step_1",
    workflow_id: partial.workflow_id ?? "wf_1",
    name: partial.name ?? "search",
    status: partial.status ?? "pending",
    attempt: partial.attempt ?? 1,
    started_at: null,
    finished_at: null,
    input: null,
    output: null,
    error: null,
    snapshot_id: null,
    created_at: "2026-01-01T00:00:00Z",
    max_attempts: partial.max_attempts ?? 1,
    retry_policy: partial.retry_policy ?? "none",
    retry_initial_delay_seconds: partial.retry_initial_delay_seconds ?? 1.0,
    retry_max_delay_seconds: partial.retry_max_delay_seconds ?? 60.0,
    retry_jitter: partial.retry_jitter ?? true,
    next_retry_at: null,
  };
}

function fakeDlqEntry(partial: Partial<DLQEntry> = {}): DLQEntry {
  return {
    id: partial.id ?? "dlqstep_a",
    step_id: partial.step_id ?? "step_1",
    workflow_id: partial.workflow_id ?? "wf_1",
    workspace_id: "ws_1",
    step_name: partial.step_name ?? "search",
    attempts: partial.attempts ?? 3,
    last_error: partial.last_error ?? "boom",
    failed_at: "2026-05-09T12:00:00Z",
    step_snapshot: partial.step_snapshot ?? { name: "search" },
  };
}

describe("WorkflowsClient.create with per-step retry config (v1.1)", () => {
  it("normalises dict-style steps into a string manifest server-side", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    let receivedBody: Record<string, unknown> | null = null;
    server.on("POST", /\/workflows$/, (req) => {
      receivedBody = JSON.parse(req.body ?? "{}");
      return { status: 201, body: fakeWorkflow() };
    });

    await ws.workflows.create("pipeline", {
      steps: [
        {
          name: "search",
          maxAttempts: 3,
          retryPolicy: "exponential",
          retryInitialDelaySeconds: 2.0,
        },
        { name: "fetch", maxAttempts: 5, retryPolicy: "exponential" },
      ],
    });

    expect(receivedBody).not.toBeNull();
    expect(receivedBody!.steps).toEqual(["search", "fetch"]);
  });

  it("forwards cached retry config to startStep", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    let stepBody: Record<string, unknown> | null = null;
    server.on("POST", /\/workflows\/[^/]+\/steps$/, (req) => {
      stepBody = JSON.parse(req.body ?? "{}");
      return { status: 201, body: fakeStep({ name: "search" }) };
    });

    const wf = await ws.workflows.create("pipeline", {
      steps: [
        {
          name: "search",
          maxAttempts: 3,
          retryPolicy: "exponential",
          retryInitialDelaySeconds: 2.0,
        },
        "fetch",
      ],
    });
    await wf.startStep("search", { initialStatus: "pending" });
    expect(stepBody!.max_attempts).toBe(3);
    expect(stepBody!.retry_policy).toBe("exponential");
    expect(stepBody!.retry_initial_delay_seconds).toBe(2.0);
  });

  it("explicit startStep options override cached retry config", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    let stepBody: Record<string, unknown> | null = null;
    server.on("POST", /\/workflows\/[^/]+\/steps$/, (req) => {
      stepBody = JSON.parse(req.body ?? "{}");
      return { status: 201, body: fakeStep({ name: "search" }) };
    });

    const wf = await ws.workflows.create("pipeline", {
      steps: [{ name: "search", maxAttempts: 3, retryPolicy: "fixed" }],
    });
    await wf.startStep("search", {
      initialStatus: "pending",
      maxAttempts: 10,
      retryPolicy: "exponential",
    });
    expect(stepBody!.max_attempts).toBe(10);
    expect(stepBody!.retry_policy).toBe("exponential");
  });

  it("string-only manifest leaves retry params off the step body", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    let stepBody: Record<string, unknown> | null = null;
    server.on("POST", /\/workflows\/[^/]+\/steps$/, (req) => {
      stepBody = JSON.parse(req.body ?? "{}");
      return { status: 201, body: fakeStep({ name: "search" }) };
    });

    const wf = await ws.workflows.create("pipeline", {
      steps: ["search", "fetch"],
    });
    await wf.startStep("search");
    expect(stepBody!).not.toHaveProperty("max_attempts");
    expect(stepBody!).not.toHaveProperty("retry_policy");
  });
});

describe("WorkflowHandle DLQ access (v1.1)", () => {
  it("dlq() returns the parsed entries", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    server.json("GET", /\/workflows\/[^/]+\/dlq$/, {
      entries: [fakeDlqEntry({ id: "dlqstep_a" }), fakeDlqEntry({ id: "dlqstep_b" })],
    });

    const wf = await ws.workflows.create("pipeline", { steps: ["search"] });
    const entries = await wf.dlq();
    expect(entries).toHaveLength(2);
    expect(entries[0]!.id).toBe("dlqstep_a");
    expect(entries[0]!.step_name).toBe("search");
  });

  it("replayDlq returns the new step and refreshes the workflow", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    server.on(
      "POST",
      /\/workflows\/[^/]+\/dlq\/dlqstep_a\/replay$/,
      () => ({
        status: 200,
        body: {
          dlq_id: "dlqstep_a",
          replayed_step: fakeStep({ id: "step_NEW", status: "pending" }),
        } satisfies DLQReplayResult,
      }),
    );
    server.on("GET", /\/workflows\/wf_1$/, () => ({
      status: 200,
      body: fakeWorkflow(),
    }));

    const wf = await ws.workflows.create("pipeline", { steps: ["search"] });
    const replayed = await wf.replayDlq("dlqstep_a");
    expect(replayed).not.toBeNull();
    expect(replayed!.id).toBe("step_NEW");
    expect(replayed!.status).toBe("pending");
  });

  it("deleteDlq fires DELETE /dlq/{id}", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    let deleted = false;
    server.on(
      "DELETE",
      /\/workflows\/[^/]+\/dlq\/dlqstep_a$/,
      () => {
        deleted = true;
        return { status: 204 };
      },
    );

    const wf = await ws.workflows.create("pipeline", { steps: ["search"] });
    await wf.deleteDlq("dlqstep_a");
    expect(deleted).toBe(true);
  });
});

describe("startStep retry param forwarding (v1.1)", () => {
  it("includes max_attempts when explicitly passed (no manifest config)", async () => {
    const server = new MockServer();
    const { ws } = await bootstrap(server);

    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow(),
    }));
    let stepBody: Record<string, unknown> | null = null;
    server.on("POST", /\/workflows\/[^/]+\/steps$/, (req) => {
      stepBody = JSON.parse(req.body ?? "{}");
      return { status: 201, body: fakeStep({ name: "search" }) };
    });

    const wf = await ws.workflows.create("pipeline", {
      steps: ["search", "fetch"],
    });
    await wf.startStep("search", {
      initialStatus: "pending",
      maxAttempts: 4,
      retryPolicy: "exponential",
      retryInitialDelaySeconds: 1.5,
      retryMaxDelaySeconds: 30.0,
      retryJitter: false,
    });
    expect(stepBody!.max_attempts).toBe(4);
    expect(stepBody!.retry_policy).toBe("exponential");
    expect(stepBody!.retry_initial_delay_seconds).toBe(1.5);
    expect(stepBody!.retry_max_delay_seconds).toBe(30.0);
    expect(stepBody!.retry_jitter).toBe(false);
  });
});
