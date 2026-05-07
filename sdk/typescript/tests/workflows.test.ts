/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import {
  InvalidWorkflowStepError,
  Plinth,
  WorkflowHandle,
  WorkflowNotFoundError,
  type ResumeInfo,
  type Workflow,
  type WorkflowStep,
  type Workspace,
} from "../src/index.js";
import { MockServer } from "./_helpers.js";

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

function fakeWorkflow(partial: Partial<Workflow> = {}): Workflow {
  return {
    id: partial.id ?? "wf_1",
    workspace_id: "ws_1",
    name: partial.name ?? "research-pipeline",
    steps_manifest: partial.steps_manifest ?? ["search", "fetch", "extract", "synthesize"],
    steps: partial.steps ?? [],
    status: partial.status ?? "pending",
    metadata: partial.metadata ?? {},
    created_at: "2026-01-01T00:00:00Z",
    started_at: partial.started_at ?? null,
    finished_at: partial.finished_at ?? null,
  };
}

function fakeStep(partial: Partial<WorkflowStep>): WorkflowStep {
  return {
    id: partial.id ?? "step_1",
    workflow_id: "wf_1",
    name: partial.name ?? "search",
    status: partial.status ?? "running",
    attempt: partial.attempt ?? 1,
    started_at: partial.started_at ?? "2026-01-01T00:00:00Z",
    finished_at: partial.finished_at ?? null,
    input: partial.input ?? null,
    output: partial.output ?? null,
    error: partial.error ?? null,
    snapshot_id: partial.snapshot_id ?? null,
    created_at: partial.created_at ?? "2026-01-01T00:00:00Z",
  };
}

describe("WorkflowsClient — create / get / list", () => {
  it("create POSTs the manifest and returns a handle", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.name).toBe("research-pipeline");
      expect(body.steps).toEqual(["search", "fetch"]);
      expect(body.metadata).toEqual({ topic: "renewables" });
      return {
        status: 201,
        body: fakeWorkflow({
          steps_manifest: body.steps,
          metadata: body.metadata,
        }),
      };
    });

    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("research-pipeline", {
      steps: ["search", "fetch"],
      metadata: { topic: "renewables" },
    });
    expect(wf).toBeInstanceOf(WorkflowHandle);
    expect(wf.id).toBe("wf_1");
    expect(wf.name).toBe("research-pipeline");
    expect(wf.stepsManifest).toEqual(["search", "fetch"]);
    expect(wf.metadata).toEqual({ topic: "renewables" });
  });

  it("get fetches a workflow by ID", async () => {
    const server = new MockServer();
    server.json("GET", /\/workflows\/wf_1$/, fakeWorkflow({ status: "running" }));
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.get("wf_1");
    expect(wf.status).toBe("running");
  });

  it("get propagates WORKFLOW_NOT_FOUND", async () => {
    const server = new MockServer();
    server.on("GET", /\/workflows\/wf_missing$/, () => ({
      status: 404,
      body: { error: { code: "WORKFLOW_NOT_FOUND", message: "no such wf" } },
    }));
    const { ws } = await bootstrap(server);
    await expect(ws.workflows.get("wf_missing")).rejects.toBeInstanceOf(WorkflowNotFoundError);
  });

  it("list returns the workflows array", async () => {
    const server = new MockServer();
    server.json("GET", /\/workflows$/, {
      workflows: [fakeWorkflow(), fakeWorkflow({ id: "wf_2", name: "writer" })],
    });
    const { ws } = await bootstrap(server);
    const all = await ws.workflows.list();
    expect(all).toHaveLength(2);
    expect(all.map((w) => w.id)).toEqual(["wf_1", "wf_2"]);
  });

  it("getOrCreate reuses an existing workflow with the same name", async () => {
    const server = new MockServer();
    server.json("GET", /\/workflows$/, {
      workflows: [fakeWorkflow({ name: "research-pipeline" })],
    });
    server.json("GET", /\/workflows\/wf_1$/, fakeWorkflow({ status: "running" }));

    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.getOrCreate("research-pipeline", { steps: ["search"] });
    expect(wf.id).toBe("wf_1");
    expect(server.requests.find((r) => r.method === "POST")).toBeUndefined();
  });

  it("getOrCreate creates a new workflow when none matches", async () => {
    const server = new MockServer();
    server.json("GET", /\/workflows$/, { workflows: [] });
    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow({ id: "wf_new", name: "fresh" }),
    }));
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.getOrCreate("fresh", { steps: ["a"] });
    expect(wf.id).toBe("wf_new");
  });
});

describe("WorkflowHandle — step transitions", () => {
  it("startStep validates the manifest client-side", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow({ steps_manifest: ["search"] }),
    }));
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    await expect(wf.startStep("not-in-manifest")).rejects.toBeInstanceOf(
      InvalidWorkflowStepError,
    );
  });

  it("startStep POSTs the body and caches the returned step", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({
      status: 201,
      body: fakeWorkflow({ steps_manifest: ["search"] }),
    }));
    server.on("POST", /\/workflows\/wf_1\/steps$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.name).toBe("search");
      expect(body.input).toEqual({ topic: "x" });
      return {
        status: 201,
        body: fakeStep({ id: "step_1", input: body.input, status: "running" }),
      };
    });
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    const step = await wf.startStep("search", { input: { topic: "x" } });
    expect(step.id).toBe("step_1");
    expect(wf.steps).toHaveLength(1);
    expect(wf.steps[0]!.id).toBe("step_1");
  });

  it("completeStep PATCHes status=completed with output + snapshot", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({ status: 201, body: fakeWorkflow() }));
    server.on("PATCH", /\/workflows\/wf_1\/steps\/step_1$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.status).toBe("completed");
      expect(body.output).toEqual({ found: 5 });
      expect(body.snapshot_id).toBe("snap_1");
      return {
        body: fakeStep({
          id: "step_1",
          status: "completed",
          output: body.output,
          snapshot_id: body.snapshot_id,
        }),
      };
    });
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    const completed = await wf.completeStep("step_1", {
      output: { found: 5 },
      snapshotId: "snap_1",
    });
    expect(completed.status).toBe("completed");
  });

  it("failStep PATCHes status=failed with error", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({ status: 201, body: fakeWorkflow() }));
    server.on("PATCH", /\/workflows\/wf_1\/steps\/step_1$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.status).toBe("failed");
      expect(body.error).toBe("connection refused");
      return { body: fakeStep({ id: "step_1", status: "failed", error: body.error }) };
    });
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    const step = await wf.failStep("step_1", "connection refused");
    expect(step.status).toBe("failed");
    expect(step.error).toBe("connection refused");
  });

  it("cancelStep PATCHes status=cancelled", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({ status: 201, body: fakeWorkflow() }));
    server.on("PATCH", /\/workflows\/wf_1\/steps\/step_1$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.status).toBe("cancelled");
      return { body: fakeStep({ id: "step_1", status: "cancelled" }) };
    });
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    const step = await wf.cancelStep("step_1");
    expect(step.status).toBe("cancelled");
  });
});

describe("WorkflowHandle — whole-workflow ops", () => {
  it("cancel POSTs cancel and refreshes the cached model", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({ status: 201, body: fakeWorkflow() }));
    server.on("POST", /\/workflows\/wf_1\/cancel$/, () => ({
      body: fakeWorkflow({ status: "cancelled" }),
    }));
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    expect(wf.status).toBe("pending");
    await wf.cancel();
    expect(wf.status).toBe("cancelled");
  });

  it("resumeInfo GETs the resume route and returns the typed body", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({ status: 201, body: fakeWorkflow() }));
    server.on("GET", /\/workflows\/wf_1\/resume$/, () => ({
      body: {
        workflow_id: "wf_1",
        workflow_status: "running",
        next_step: "fetch",
        last_completed: fakeStep({ name: "search", status: "completed" }),
        snapshot_id: "snap_1",
      } satisfies ResumeInfo,
    }));
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search", "fetch"] });
    const resume = await wf.resumeInfo();
    expect(resume.next_step).toBe("fetch");
    expect(resume.snapshot_id).toBe("snap_1");
  });

  it("refresh re-reads the workflow body and updates status/steps", async () => {
    const server = new MockServer();
    server.on("POST", /\/workflows$/, () => ({ status: 201, body: fakeWorkflow() }));
    server.on("GET", /\/workflows\/wf_1$/, () => ({
      body: fakeWorkflow({
        status: "running",
        steps: [fakeStep({ id: "step_1", status: "completed" })],
      }),
    }));
    const { ws } = await bootstrap(server);
    const wf = await ws.workflows.create("p", { steps: ["search"] });
    expect(wf.steps).toHaveLength(0);
    await wf.refresh();
    expect(wf.status).toBe("running");
    expect(wf.steps).toHaveLength(1);
  });
});
