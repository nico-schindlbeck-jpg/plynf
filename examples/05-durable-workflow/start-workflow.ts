/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Driver for the durable-workflow demo — TypeScript counterpart of
 * `start_workflow.py`.
 *
 * 1. Creates / fetches the `durable-demo` workspace.
 * 2. Idempotently creates a `research-pipeline` workflow with the
 *    manifest `search → fetch → extract → synth`.
 * 3. Creates each step in `pending` status so workers can lease them.
 * 4. Polls until the workflow transitions to `completed` / `failed`.
 * 5. Reads `report.md` from the workspace and prints it.
 *
 * Run AFTER starting at least one `plinth-workflow-worker`:
 *
 *     plinth-workflow-worker --handlers-module ./handlers.js --concurrency 2
 *
 * Then in another terminal:
 *
 *     node ./start-workflow.js --topic "renewable energy"
 *
 * You can kill the worker mid-flight; start another one — it will pick
 * up where the first left off.
 */

import { parseArgs } from "node:util";

import { Plinth, type WorkflowHandle } from "@plinth/sdk";

import { makeClientKwargs, servicesAvailable } from "./shared.js";

const WORKFLOW_NAME = "research-pipeline";
const WORKFLOW_STEPS = ["search", "fetch", "extract", "synth"];

async function ensureServices(): Promise<void> {
  const services = await servicesAvailable();
  const missing = Object.entries(services)
    .filter(([, ok]) => !ok)
    .map(([k]) => k);
  if (missing.length > 0) {
    process.stderr.write(
      `[start] services not reachable: ${missing.join(", ")}. ` +
        "Start them with `make services` then retry.\n",
    );
    process.exit(2);
  }
}

async function ensurePendingSteps(wf: WorkflowHandle, topic: string): Promise<number> {
  await wf.refresh();
  const completed = new Set(
    wf.steps.filter((s) => s.status === "completed").map((s) => s.name),
  );
  const inflight = new Set(
    wf.steps
      .filter((s) => s.status === "running" || s.status === "pending")
      .map((s) => s.name),
  );
  let started = 0;
  for (const name of WORKFLOW_STEPS) {
    if (completed.has(name) || inflight.has(name)) continue;
    // ``initialStatus: "pending"`` is the v0.5 opt-in: the step is
    // staged for a worker to lease rather than running in-process.
    await wf.startStep(name, {
      input: { topic, k: 5 },
      initialStatus: "pending",
    });
    started += 1;
  }
  return started;
}

async function main(): Promise<number> {
  const { values } = parseArgs({
    options: {
      topic: { type: "string", default: "renewable energy" },
      "workspace-name": { type: "string", default: "durable-demo" },
      timeout: { type: "string", default: "120" },
      "poll-interval": { type: "string", default: "2" },
    },
  });
  const topic = values.topic as string;
  const wsName = values["workspace-name"] as string;
  const timeout = Number.parseInt(values.timeout as string, 10);
  const pollInterval = Number.parseFloat(values["poll-interval"] as string) * 1000;

  await ensureServices();

  const cfg = makeClientKwargs();
  const client = new Plinth({
    workspaceUrl: cfg.workspaceUrl,
    gatewayUrl: cfg.gatewayUrl,
    apiKey: cfg.apiKey,
  });
  const ws = await client.workspace(wsName);
  process.stdout.write(`[start] workspace: ${ws.id} (${ws.name})\n`);

  const wf = await ws.workflows.getOrCreate(WORKFLOW_NAME, {
    steps: WORKFLOW_STEPS,
  });
  process.stdout.write(`[start] workflow: ${wf.id} (status=${wf.status})\n`);

  const started = await ensurePendingSteps(wf, topic);
  if (started > 0) {
    process.stdout.write(`[start] queued ${started} steps for the worker pool\n`);
  } else {
    process.stdout.write("[start] all steps already in flight or done\n");
  }

  const deadline = Date.now() + timeout * 1000;
  let lastStatus: string | null = null;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    await wf.refresh();
    const status = wf.status;
    if (status !== lastStatus) {
      const done = wf.steps.filter((s) => s.status === "completed").length;
      const running = wf.steps.filter((s) => s.status === "running").length;
      const pending = wf.steps.filter((s) => s.status === "pending").length;
      process.stdout.write(
        `[start] status=${status} completed=${done} running=${running} pending=${pending}\n`,
      );
      lastStatus = status;
    }
    if (status === "completed" || status === "failed" || status === "cancelled") {
      process.stdout.write(`[start] workflow ${wf.id} ${status}\n`);
      if (status === "completed") {
        try {
          const report = await ws.files.readText("report.md");
          process.stdout.write("---- report.md ----\n");
          process.stdout.write(report);
          process.stdout.write("\n");
        } catch (err) {
          process.stdout.write(
            `[start] could not read report.md: ${(err as Error).message}\n`,
          );
        }
      }
      return status === "completed" ? 0 : 1;
    }
    if (Date.now() > deadline) {
      process.stdout.write(
        `[start] TIMEOUT after ${timeout}s; current status=${status}\n`,
      );
      return 1;
    }
    await new Promise<void>((r) => setTimeout(r, pollInterval));
  }
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    process.stderr.write(`[start] error: ${(err as Error).message}\n`);
    process.exit(1);
  });
