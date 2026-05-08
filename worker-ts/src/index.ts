/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Public entry point for `@plinth/workflow-worker`.
 *
 * Embed the runtime + worker in an existing process when you don't want
 * to ship a separate CLI binary:
 *
 *     import { Plinth } from "@plinth/sdk";
 *     import { Worker, WorkflowRuntime } from "@plinth/workflow-worker";
 *
 *     const client = new Plinth({ workspaceUrl, gatewayUrl, apiKey });
 *     const runtime = new WorkflowRuntime();
 *     runtime.register("research-pipeline", "search", async (ctx) => { ... });
 *
 *     const worker = new Worker({ client, runtime, concurrency: 2 });
 *     process.once("SIGTERM", () => worker.stop());
 *     await worker.run();
 */

export {
  WorkflowRuntime,
  buildHandlerContext,
} from "./runtime.js";
export type { HandlerContext, RegisteredHandler, WorkflowHandler } from "./runtime.js";

export { Worker, defaultLogger } from "./worker.js";
export type {
  WorkerLogger,
  WorkerOptions,
  WorkerStats,
} from "./worker.js";

export { loadHandlers, resolveModulePath } from "./handlers-loader.js";
export type { HandlersModule } from "./handlers-loader.js";

// Re-export NoHandlerError so consumers don't need a separate
// `@plinth/sdk` import just to catch it.
export { NoHandlerError } from "@plinth/sdk";
