#!/usr/bin/env node
/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * CLI entry point — `plinth-workflow-worker`.
 *
 * Mirrors the Python `python -m plinth_workflow_worker` CLI, with one
 * surface difference: handler registration is explicit
 * (`register(runtime, client)`) rather than decorator-driven, because
 * ESM doesn't make import-time side effects reliable across bundlers.
 *
 * Usage:
 *
 *     plinth-workflow-worker \
 *       --workspace-url http://localhost:7421 \
 *       --gateway-url http://localhost:7422 \
 *       --api-key local-dev \
 *       --concurrency 4 \
 *       --lease-ttl 60 \
 *       --heartbeat-interval 15 \
 *       --handlers-module ./handlers.js
 */

import { parseArgs } from "node:util";

import { Plinth } from "@plinth/sdk";

import { loadHandlers } from "./handlers-loader.js";
import { WorkflowRuntime } from "./runtime.js";
import { Worker } from "./worker.js";

const VERSION = "0.6.1";

const USAGE = `plinth-workflow-worker — Plinth durable workflow worker (Node.js)

Usage:
  plinth-workflow-worker [options]

Options:
  --workspace-url URL          Workspace service URL (default: http://localhost:7421
                               or $PLINTH_WORKSPACE_URL).
  --gateway-url URL            Gateway service URL (default: http://localhost:7422
                               or $PLINTH_GATEWAY_URL).
  --identity-url URL           Identity service URL (optional, $PLINTH_IDENTITY_URL).
  --api-key TOKEN              Bearer token for both services (default: local-dev or
                               $PLINTH_API_KEY).
  --concurrency N              Number of concurrent in-flight steps (default: 4).
  --lease-ttl SECONDS          Lease TTL when leasing (default: 60).
  --heartbeat-interval SEC     Per-lease heartbeat interval (default: 15).
  --worker-heartbeat-interval SEC
                               Worker-level heartbeat interval (default: 30).
  --poll-interval SECONDS      Idle poll interval when no work is available
                               (default: 2).
  --handlers-module SPEC       Path or package name of a module exporting
                               'register(runtime, client)'. Required.
  --workspace NAME_OR_ID       Restrict to the named workspace (can be passed
                               multiple times). When omitted, every workspace
                               visible to the API key is scanned.
  --silent                     Suppress info/warning logs.
  --version                    Print version and exit.
  -h, --help                   Print this help and exit.
`;

interface CliArgs {
  workspaceUrl: string;
  gatewayUrl: string;
  identityUrl?: string;
  apiKey: string;
  concurrency: number;
  leaseTtl: number;
  heartbeatInterval: number;
  workerHeartbeatInterval: number;
  pollInterval: number;
  handlersModule: string;
  workspace: string[];
  silent: boolean;
}

function env(name: string, fallback: string): string {
  const v = process.env[name];
  return v && v.length > 0 ? v : fallback;
}

function envOr(name: string): string | undefined {
  const v = process.env[name];
  return v && v.length > 0 ? v : undefined;
}

function parseInteger(name: string, raw: string | undefined, fallback: number): number {
  if (raw === undefined) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || Number.isNaN(parsed)) {
    throw new Error(`--${name} must be an integer; got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

function parseFloatArg(name: string, raw: string | undefined, fallback: number): number {
  if (raw === undefined) return fallback;
  const parsed = Number.parseFloat(raw);
  if (!Number.isFinite(parsed) || Number.isNaN(parsed)) {
    throw new Error(`--${name} must be a number; got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

/**
 * Parse argv into a typed CLI args object.
 *
 * Layered: `process.env.PLINTH_*` → defaults → CLI flags. CLI overrides
 * env, env overrides defaults. Mirrors the Python `_build_settings`.
 */
export function parseCli(argv: string[]): CliArgs {
  const { values } = parseArgs({
    args: argv,
    options: {
      "workspace-url": { type: "string" },
      "gateway-url": { type: "string" },
      "identity-url": { type: "string" },
      "api-key": { type: "string" },
      concurrency: { type: "string" },
      "lease-ttl": { type: "string" },
      "heartbeat-interval": { type: "string" },
      "worker-heartbeat-interval": { type: "string" },
      "poll-interval": { type: "string" },
      "handlers-module": { type: "string" },
      workspace: { type: "string", multiple: true },
      silent: { type: "boolean" },
      version: { type: "boolean" },
      help: { type: "boolean", short: "h" },
    },
    allowPositionals: false,
  });

  if (values.help === true) {
    process.stdout.write(USAGE);
    process.exit(0);
  }
  if (values.version === true) {
    process.stdout.write(`plinth-workflow-worker ${VERSION}\n`);
    process.exit(0);
  }

  const workspaceUrl = (values["workspace-url"] as string | undefined) ??
    env("PLINTH_WORKSPACE_URL", "http://localhost:7421");
  const gatewayUrl = (values["gateway-url"] as string | undefined) ??
    env("PLINTH_GATEWAY_URL", "http://localhost:7422");
  const identityUrl = (values["identity-url"] as string | undefined) ?? envOr("PLINTH_IDENTITY_URL");
  const apiKey = (values["api-key"] as string | undefined) ?? env("PLINTH_API_KEY", "local-dev");
  const concurrency = parseInteger(
    "concurrency",
    (values.concurrency as string | undefined) ?? envOr("PLINTH_CONCURRENCY"),
    4,
  );
  const leaseTtl = parseInteger(
    "lease-ttl",
    (values["lease-ttl"] as string | undefined) ?? envOr("PLINTH_LEASE_TTL"),
    60,
  );
  const heartbeatInterval = parseInteger(
    "heartbeat-interval",
    (values["heartbeat-interval"] as string | undefined) ?? envOr("PLINTH_HEARTBEAT_INTERVAL"),
    15,
  );
  const workerHeartbeatInterval = parseInteger(
    "worker-heartbeat-interval",
    (values["worker-heartbeat-interval"] as string | undefined) ??
      envOr("PLINTH_WORKER_HEARTBEAT_INTERVAL"),
    30,
  );
  const pollInterval = parseFloatArg(
    "poll-interval",
    (values["poll-interval"] as string | undefined) ?? envOr("PLINTH_POLL_INTERVAL"),
    2,
  );
  const handlersModule = (values["handlers-module"] as string | undefined) ??
    env("PLINTH_HANDLERS_MODULE", "");
  const workspace = (values.workspace as string[] | undefined) ?? [];
  const silent = values.silent === true;

  return {
    workspaceUrl,
    gatewayUrl,
    identityUrl,
    apiKey,
    concurrency,
    leaseTtl,
    heartbeatInterval,
    workerHeartbeatInterval,
    pollInterval,
    handlersModule,
    workspace,
    silent,
  };
}

/** Programmatic entry — exported for tests. */
export async function runCli(argv: string[]): Promise<number> {
  let args: CliArgs;
  try {
    args = parseCli(argv);
  } catch (err) {
    process.stderr.write(`error: ${(err as Error).message}\n${USAGE}`);
    return 2;
  }

  if (!args.handlersModule) {
    process.stderr.write(
      "error: --handlers-module is required (or set PLINTH_HANDLERS_MODULE).\n" +
        "Pass the path to a module that exports 'register(runtime, client)'.\n",
    );
    return 2;
  }

  const client = new Plinth({
    workspaceUrl: args.workspaceUrl,
    gatewayUrl: args.gatewayUrl,
    ...(args.identityUrl !== undefined ? { identityUrl: args.identityUrl } : {}),
    apiKey: args.apiKey,
  });

  const runtime = new WorkflowRuntime();
  await loadHandlers(args.handlersModule, runtime, client);

  const worker = new Worker({
    client,
    runtime,
    concurrency: args.concurrency,
    leaseTtlSeconds: args.leaseTtl,
    heartbeatIntervalSeconds: args.heartbeatInterval,
    workerHeartbeatIntervalSeconds: args.workerHeartbeatInterval,
    pollIntervalSeconds: args.pollInterval,
    workspaceFilter: args.workspace.length > 0 ? args.workspace : null,
    logger: args.silent ? null : undefined,
  });

  let stopRequested = false;
  const stop = (): void => {
    if (stopRequested) return;
    stopRequested = true;
    worker.stop().catch(() => {
      /* swallow — best-effort shutdown */
    });
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);

  if (!args.silent) {
    process.stderr.write(
      `[plinth-workflow-worker] starting — handlers: ${runtime.size()}, ` +
        `concurrency: ${args.concurrency}\n`,
    );
  }
  await worker.run();
  if (!args.silent) {
    process.stderr.write("[plinth-workflow-worker] drained.\n");
  }
  return 0;
}

/** Module-as-script entry point. */
async function main(): Promise<void> {
  const code = await runCli(process.argv.slice(2));
  process.exit(code);
}

// Detect direct execution: tsc emits this file as ESM, so we compare the
// import meta URL against the entry-point file URL rather than checking
// `require.main === module`.
const entry = process.argv[1] ? new URL(`file://${process.argv[1]}`).href : null;
if (entry !== null && import.meta.url === entry) {
  void main();
}
