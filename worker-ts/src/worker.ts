/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Durable workflow worker — Node/TypeScript port of `plinth-workflow-worker`.
 *
 * Responsibilities (mirroring the Python reference at
 * `worker/src/plinth_workflow_worker/worker.py`):
 *
 *   1. Register with the workspace via `client.workers.register()` to
 *      obtain a `worker_id`.
 *   2. Spawn a worker-level heartbeat so the workspace's reaper doesn't
 *      sweep us to `gone`.
 *   3. Spawn `concurrency` slot loops. Each slot independently:
 *      a. Discovers workspaces + workflows visible to the API key.
 *      b. Polls `wf.pendingSteps()` for steps whose `(name, step)` is
 *         in the runtime's dispatch table.
 *      c. Tries `wf.leaseStep(...)` — `null` on a 409 means another
 *         worker beat us; the slot moves on without blocking.
 *      d. Runs the handler. While running, a per-lease heartbeat
 *         bumps the lease's `expires_at` so the reaper doesn't reclaim
 *         the step mid-flight.
 *      e. Releases the lease with `status = completed | failed` based
 *         on whether the handler returned or threw.
 *   4. On `stop()` (graceful shutdown): set the stop flag, wait for
 *      slots to drain, drain the worker (`status=draining`).
 *
 * Steps whose `(workflow_name, step_name)` is *not* in the runtime are
 * left alone — another worker with the right handlers can pick them up.
 */

import {
  LeaseConflictError,
  PlinthError,
  WorkflowNotFoundError,
  type Plinth,
  type Workspace,
  type WorkflowHandle,
  type WorkflowStep,
  type WorkspaceRecord,
} from "@plinth/sdk";

import { buildHandlerContext, type WorkflowRuntime } from "./runtime.js";

/** Counters captured during a worker's lifetime. */
export interface WorkerStats {
  /** Steps successfully leased (regardless of handler outcome). */
  leased: number;
  /** Steps where the handler returned cleanly. */
  completed: number;
  /** Steps where the handler threw. */
  failed: number;
  /** Lease attempts that lost to another worker (409 LEASE_CONFLICT). */
  lost: number;
}

/** Constructor options for {@link Worker}. */
export interface WorkerOptions {
  /** A configured `Plinth` SDK client. */
  client: Plinth;
  /** Handler registry — populated before the worker boots. */
  runtime: WorkflowRuntime;
  /**
   * Number of in-flight steps the worker holds at once. Each slot polls
   * + leases + executes independently. Default `4`.
   */
  concurrency?: number;
  /**
   * TTL passed when acquiring a lease (seconds). Must be `>` the
   * heartbeat interval so a missed beat doesn't expire the lease.
   * Default `60`.
   */
  leaseTtlSeconds?: number;
  /** Seconds between per-lease heartbeats. Default `15`. */
  heartbeatIntervalSeconds?: number;
  /** Seconds between worker-level heartbeats to `/v1/workers`. Default `30`. */
  workerHeartbeatIntervalSeconds?: number;
  /** Seconds the worker sleeps when no work is available. Default `2`. */
  pollIntervalSeconds?: number;
  /**
   * Optional whitelist of workspace IDs/names. When `undefined` (the
   * default), every workspace visible to the API key is scanned.
   */
  workspaceFilter?: string[] | null;
  /**
   * Optional logger. Defaults to a `console.*` adapter; pass `null` for
   * silent operation.
   */
  logger?: WorkerLogger | null;
}

/** Minimum logging surface — easy to swap for pino, winston, etc. */
export interface WorkerLogger {
  info(event: string, fields?: Record<string, unknown>): void;
  warn(event: string, fields?: Record<string, unknown>): void;
  debug(event: string, fields?: Record<string, unknown>): void;
}

/**
 * Default logger: prints `{event, ...fields}` JSON to stderr at info+ /
 * stderr at warn (debug is dropped). Mirrors the Python logger's output
 * style without depending on structlog.
 */
export function defaultLogger(): WorkerLogger {
  return {
    info(event, fields) {
      // eslint-disable-next-line no-console
      console.error(JSON.stringify({ level: "info", event, ...fields }));
    },
    warn(event, fields) {
      // eslint-disable-next-line no-console
      console.error(JSON.stringify({ level: "warn", event, ...fields }));
    },
    debug() {
      // No-op — debug events are dropped by default.
    },
  };
}

const SILENT_LOGGER: WorkerLogger = {
  info() {},
  warn() {},
  debug() {},
};

/**
 * Durable workflow worker.
 *
 * Construct one per process; call {@link run} to start polling, and
 * {@link stop} to drain. The constructor validates that
 * `heartbeat_interval < lease_ttl`.
 */
export class Worker {
  /** Server-assigned worker ID after registration. */
  workerId: string | null = null;

  readonly concurrency: number;
  readonly leaseTtlSeconds: number;
  readonly heartbeatIntervalSeconds: number;
  readonly workerHeartbeatIntervalSeconds: number;
  readonly pollIntervalSeconds: number;
  readonly workspaceFilter: string[] | null;

  private readonly client: Plinth;
  private readonly runtime: WorkflowRuntime;
  private readonly log: WorkerLogger;

  private stopping = false;
  private slotPromises: Promise<void>[] = [];
  private workerHeartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private readonly waiters: Array<() => void> = [];
  private readonly stats: WorkerStats = {
    leased: 0,
    completed: 0,
    failed: 0,
    lost: 0,
  };

  constructor(opts: WorkerOptions) {
    const concurrency = opts.concurrency ?? 4;
    const leaseTtl = opts.leaseTtlSeconds ?? 60;
    const heartbeat = opts.heartbeatIntervalSeconds ?? 15;
    const workerHeartbeat = opts.workerHeartbeatIntervalSeconds ?? 30;
    const pollInterval = opts.pollIntervalSeconds ?? 2;

    if (concurrency < 1) {
      throw new Error("concurrency must be >= 1");
    }
    if (heartbeat >= leaseTtl) {
      throw new Error(
        "heartbeatIntervalSeconds must be < leaseTtlSeconds, otherwise the " +
          "lease will expire between heartbeats",
      );
    }

    this.client = opts.client;
    this.runtime = opts.runtime;
    this.concurrency = concurrency;
    this.leaseTtlSeconds = leaseTtl;
    this.heartbeatIntervalSeconds = heartbeat;
    this.workerHeartbeatIntervalSeconds = workerHeartbeat;
    this.pollIntervalSeconds = pollInterval;
    this.workspaceFilter =
      opts.workspaceFilter !== undefined && opts.workspaceFilter !== null
        ? [...opts.workspaceFilter]
        : null;
    this.log = opts.logger === null ? SILENT_LOGGER : (opts.logger ?? defaultLogger());
  }

  /** Snapshot of execution counters. */
  getStats(): WorkerStats {
    return { ...this.stats };
  }

  /**
   * Run the worker until {@link stop} is called.
   *
   * Resolves once every slot loop has finished and the worker has been
   * drained on the server.
   */
  async run(): Promise<void> {
    if (this.runtime.size() === 0) {
      this.log.warn("worker.no_handlers", {
        hint:
          "no handlers registered; the worker will register but never claim work",
      });
    }

    const registration = await this.client.workers.register();
    this.workerId = registration.id;
    this.log.info("worker.registered", {
      workerId: this.workerId,
      handlers: this.runtime.list(),
      concurrency: this.concurrency,
    });

    // Worker-level heartbeat keeps the reaper from sweeping us.
    this.workerHeartbeatTimer = setInterval(() => {
      this.sendWorkerHeartbeat().catch((err: unknown) => {
        this.log.warn("worker.heartbeat.error", {
          error: err instanceof Error ? err.message : String(err),
        });
      });
    }, this.workerHeartbeatIntervalSeconds * 1000);
    // Don't keep the Node process alive on the heartbeat alone.
    const handle = this.workerHeartbeatTimer as { unref?: () => void };
    if (typeof handle.unref === "function") handle.unref();

    // Spawn slot loops. Each one runs until stop() flips the flag.
    this.slotPromises = [];
    for (let i = 0; i < this.concurrency; i++) {
      this.slotPromises.push(this.slotLoop(i));
    }

    try {
      await Promise.all(this.slotPromises);
    } finally {
      await this.shutdown();
    }
  }

  /**
   * Signal a graceful shutdown.
   *
   * Idempotent. Slot loops finish their current step (if any), then
   * exit; {@link run} resolves once all slots have drained and the
   * worker has been drained on the server.
   */
  async stop(): Promise<void> {
    this.stopping = true;
    // Wake any sleeping slot waiters so the loop can re-check `stopping`.
    while (this.waiters.length > 0) {
      const waiter = this.waiters.shift()!;
      waiter();
    }
  }

  // ------------------------------------------------------------------
  // Slot loop — public for tests
  // ------------------------------------------------------------------

  /**
   * Try one poll → lease → execute → release iteration. Returns `true`
   * iff a step was successfully claimed (and either completed or
   * failed) this iteration.
   *
   * Exposed so tests can drive the worker through one cycle without
   * waiting for the slot loop's idle backoff.
   */
  async pollLeaseAndExecute(): Promise<boolean> {
    if (!this.workerId) {
      throw new Error("worker has not registered yet — call run() first");
    }
    const candidate = await this.nextCandidate();
    if (candidate === null) return false;

    const { workspace, workspaceRecord, workflow, step } = candidate;
    const stepId = step.id;

    let lease;
    try {
      lease = await workflow.leaseStep(stepId, this.workerId, {
        ttlSeconds: this.leaseTtlSeconds,
      });
    } catch (err) {
      if (err instanceof LeaseConflictError) {
        // Defensive: shouldn't happen — leaseStep maps 409 to null.
        this.stats.lost += 1;
        return false;
      }
      throw err;
    }
    if (lease === null) {
      this.stats.lost += 1;
      this.log.debug("worker.lease.lost", {
        stepId,
        workflowId: workflow.id,
      });
      return false;
    }

    this.stats.leased += 1;
    this.log.info("worker.lease.acquired", {
      stepId,
      workflowId: workflow.id,
      workspaceId: workspace.id,
    });

    // Refresh the workflow so the cached step shows `running` (server
    // transitions on lease).
    let runningStep = step;
    try {
      await workflow.refresh();
      const refreshed = workflow.steps.find((s) => s.id === stepId);
      if (refreshed) runningStep = refreshed;
    } catch {
      // Best-effort — if refresh fails, the original snapshot is fine.
    }

    const ctx = buildHandlerContext({
      client: this.client,
      workspaceRecord,
      workspace,
      workflow,
      step: runningStep,
      workerId: this.workerId,
    });

    // Per-lease heartbeat keeps expires_at fresh while the handler runs.
    const heartbeatTimer = setInterval(() => {
      workflow
        .heartbeatStep(stepId, this.workerId!, { ttlSeconds: this.leaseTtlSeconds })
        .catch((err: unknown) => {
          this.log.warn("worker.lease.heartbeat.error", {
            stepId,
            error: err instanceof Error ? err.message : String(err),
          });
        });
    }, this.heartbeatIntervalSeconds * 1000);
    const hbHandle = heartbeatTimer as { unref?: () => void };
    if (typeof hbHandle.unref === "function") hbHandle.unref();

    try {
      let output: unknown;
      try {
        output = await this.runtime.dispatch(workflow.name, runningStep.name, ctx);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        this.log.warn("worker.handler.failed", {
          stepId,
          stepName: runningStep.name,
          error: message,
        });
        this.stats.failed += 1;
        await this.safeRelease(workflow, stepId, { status: "failed", error: message });
        return true;
      }

      await this.safeRelease(workflow, stepId, {
        status: "completed",
        output: toJsonValue(output),
      });
      this.stats.completed += 1;
      this.log.info("worker.step.completed", {
        stepId,
        stepName: runningStep.name,
      });
      return true;
    } finally {
      clearInterval(heartbeatTimer);
    }
  }

  // ------------------------------------------------------------------
  // Internal: candidate discovery
  // ------------------------------------------------------------------

  private async nextCandidate(): Promise<{
    workspace: Workspace;
    workspaceRecord: WorkspaceRecord;
    workflow: WorkflowHandle;
    step: WorkflowStep;
  } | null> {
    let workspaces: WorkspaceRecord[];
    try {
      workspaces = await this.client.listWorkspaces();
    } catch (err) {
      this.log.warn("worker.list_workspaces.error", {
        error: err instanceof Error ? err.message : String(err),
      });
      return null;
    }

    for (const wsRecord of workspaces) {
      if (this.workspaceFilter !== null) {
        if (
          !this.workspaceFilter.includes(wsRecord.id) &&
          !this.workspaceFilter.includes(wsRecord.name)
        ) {
          continue;
        }
      }

      const workspace = await this.client.getWorkspace(wsRecord.id).catch(() => null);
      if (workspace === null) continue;

      let workflows;
      try {
        workflows = await workspace.workflows.list();
      } catch {
        continue;
      }

      for (const wfSummary of workflows) {
        const handlerSteps = new Set<string>();
        for (const { workflow: wn, step: sn } of this.runtime.list()) {
          if (wn === wfSummary.name) handlerSteps.add(sn);
        }
        if (handlerSteps.size === 0) continue;

        let workflow;
        try {
          workflow = await workspace.workflows.get(wfSummary.id);
        } catch (err) {
          if (err instanceof WorkflowNotFoundError) continue;
          continue;
        }

        let pending: WorkflowStep[];
        try {
          pending = await workflow.pendingSteps();
        } catch {
          continue;
        }

        for (const step of pending) {
          if (handlerSteps.has(step.name)) {
            return { workspace, workspaceRecord: wsRecord, workflow, step };
          }
        }
      }
    }
    return null;
  }

  // ------------------------------------------------------------------
  // Internal: slot loop, heartbeat, shutdown
  // ------------------------------------------------------------------

  private async slotLoop(slotIdx: number): Promise<void> {
    while (!this.stopping) {
      let claimed = false;
      try {
        claimed = await this.pollLeaseAndExecute();
      } catch (err) {
        this.log.warn("worker.slot.error", {
          slot: slotIdx,
          error: err instanceof Error ? err.message : String(err),
        });
      }
      if (!claimed && !this.stopping) {
        await this.idleSleep();
      }
    }
  }

  private async idleSleep(): Promise<void> {
    if (this.stopping) return;
    const ms = this.pollIntervalSeconds * 1000;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        // Drop the cancelled-resolver from the waiter queue to avoid leaks.
        const i = this.waiters.indexOf(cancel);
        if (i >= 0) this.waiters.splice(i, 1);
        resolve();
      }, ms);
      const handle = timer as { unref?: () => void };
      if (typeof handle.unref === "function") handle.unref();
      const cancel = (): void => {
        clearTimeout(timer);
        resolve();
      };
      this.waiters.push(cancel);
    });
  }

  private async sendWorkerHeartbeat(): Promise<void> {
    if (!this.workerId) return;
    await this.client.workers.heartbeat(this.workerId);
  }

  private async safeRelease(
    workflow: WorkflowHandle,
    stepId: string,
    opts: { status: "completed" | "failed"; output?: import("@plinth/sdk").JsonValue; error?: string },
  ): Promise<void> {
    try {
      await workflow.releaseStep(stepId, this.workerId!, opts);
    } catch (err) {
      if (err instanceof PlinthError) {
        this.log.warn("worker.release.error", {
          stepId,
          status: opts.status,
          error: err.message,
        });
      } else {
        throw err;
      }
    }
  }

  private async shutdown(): Promise<void> {
    if (this.workerHeartbeatTimer !== null) {
      clearInterval(this.workerHeartbeatTimer);
      this.workerHeartbeatTimer = null;
    }
    if (this.workerId !== null) {
      try {
        await this.client.workers.drain(this.workerId);
        this.log.info("worker.drained", { workerId: this.workerId });
      } catch (err) {
        this.log.warn("worker.drain.error", {
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Best-effort coercion of an arbitrary handler return value into a
 * `JsonValue` shape acceptable to the workspace API.
 *
 * Handlers that return `undefined` are normalised to `null`. Functions,
 * symbols, and other non-serialisable values pass through to the JSON
 * layer's stringifier — which will raise a clear error rather than
 * silently dropping data.
 */
function toJsonValue(value: unknown): import("@plinth/sdk").JsonValue {
  if (value === undefined) return null;
  return value as import("@plinth/sdk").JsonValue;
}
