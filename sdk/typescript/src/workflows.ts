/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Workspace workflows — durable, resumable agent pipelines.
 *
 * A workflow is a manifest of expected step names plus a server-tracked
 * log of completed steps. Each step has a lifecycle of
 * `pending -> running -> (completed | failed | cancelled)` and may
 * reference a workspace snapshot at completion time so a crashed agent
 * can resume from a known checkpoint.
 *
 * Two public surfaces:
 *
 *   * {@link WorkflowsClient} — reachable via `ws.workflows`. Owns
 *     `create` / `getOrCreate` / `get` / `list` and returns
 *     {@link WorkflowHandle} objects (never the bare model) so callers
 *     can chain step transitions ergonomically.
 *   * {@link WorkflowHandle} — wraps a {@link Workflow} and exposes
 *     method-style step transitions plus `refresh()`/`resumeInfo()`.
 *
 * Mirrors `plinth.workflows` in the Python SDK.
 */

import {
  InvalidWorkflowStepError,
  LeaseConflictError,
} from "./errors.js";
import { encodePath, type HttpClient } from "./http.js";
import type {
  DLQEntry,
  DLQReplayResult,
  JsonValue,
  Lease,
  ResumeInfo,
  Workflow,
  WorkflowRetryPolicy,
  WorkflowStatus,
  WorkflowStep,
} from "./types.js";

/**
 * v1.1 — per-step retry config that can be supplied either at workflow
 * create time (via {@link WorkflowCreateOptions.steps}) or at step
 * start time (via {@link StartStepOptions}).
 *
 * Defaults preserve v1.0 behaviour: ``maxAttempts: 1`` (no retry).
 */
export interface StepRetryConfig {
  maxAttempts?: number;
  retryPolicy?: WorkflowRetryPolicy;
  retryInitialDelaySeconds?: number;
  retryMaxDelaySeconds?: number;
  retryJitter?: boolean;
}

/** A manifest entry: either a bare step name or a name + retry config. */
export type WorkflowStepSpec = string | ({ name: string } & StepRetryConfig);

/** Options accepted by {@link WorkflowsClient.create} / `getOrCreate`. */
export interface WorkflowCreateOptions {
  /**
   * Ordered list of step specs — the manifest. Either a list of
   * strings (v1.0 behaviour) or an array of `{ name, ...retry }`
   * objects (v1.1) where the retry config is forwarded to each step
   * automatically when {@link WorkflowHandle.startStep} is called for
   * that name.
   */
  steps: ReadonlyArray<WorkflowStepSpec>;
  /** Optional free-form metadata (topic, parent run ID, etc.). */
  metadata?: Record<string, JsonValue>;
}

/** Options accepted by {@link WorkflowHandle.startStep}. */
export interface StartStepOptions extends StepRetryConfig {
  /** Optional input payload to record on the step. */
  input?: JsonValue;
  /** Optional snapshot taken before the step ran. */
  snapshotId?: string;
  /**
   * Initial step status. Defaults to `"running"` for the v0.2 in-process
   * flow. Pass `"pending"` to stage the step for a v0.5 durable worker
   * to lease and execute.
   */
  initialStatus?: "running" | "pending";
}

/** Options accepted by {@link WorkflowHandle.completeStep}. */
export interface CompleteStepOptions {
  /** Optional output payload to record on the step. */
  output?: JsonValue;
  /** Snapshot taken at step completion — the canonical resume point. */
  snapshotId?: string;
}

/**
 * Method-style wrapper around a {@link Workflow}.
 *
 * Returned by every {@link WorkflowsClient} method except `list`. Holds a
 * reference to the parent workspace's HTTP client so callers don't have
 * to thread anything through. Mutations (`startStep`, `completeStep`,
 * etc.) update the cached model so subsequent reads against the handle
 * are consistent.
 */
export class WorkflowHandle {
  private wf: Workflow;
  /**
   * v1.1 — per-step retry config supplied at workflow create time.
   * Looked up by step name in {@link startStep} so callers don't have
   * to repeat the config on every call. Empty for v1.0-style
   * `string[]` manifests.
   */
  private readonly retryConfig: Map<string, StepRetryConfig> = new Map();

  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
    model: Workflow,
    retryConfig?: ReadonlyMap<string, StepRetryConfig>,
  ) {
    this.wf = model;
    if (retryConfig !== undefined) {
      retryConfig.forEach((cfg, name) => this.retryConfig.set(name, cfg));
    }
  }

  /** Workflow ID (`wf_<ulid>`). */
  get id(): string {
    return this.wf.id;
  }

  /** Human-readable name supplied at creation. */
  get name(): string {
    return this.wf.name;
  }

  /** Current workflow status from the cached model. */
  get status(): WorkflowStatus {
    return this.wf.status;
  }

  /** Cached step log. */
  get steps(): WorkflowStep[] {
    return this.wf.steps;
  }

  /** Expected step names in declaration order. */
  get stepsManifest(): string[] {
    return this.wf.steps_manifest;
  }

  /** Free-form metadata dict supplied at creation. */
  get metadata(): Record<string, JsonValue> {
    return this.wf.metadata;
  }

  /** Underlying {@link Workflow} model (cached). */
  get model(): Workflow {
    return this.wf;
  }

  // -- step transitions ------------------------------------------------

  /**
   * Create and start a step on this workflow.
   *
   * Validates `name` against {@link stepsManifest} client-side so the
   * caller gets a synchronous error before paying for the HTTP roundtrip.
   *
   * @throws InvalidWorkflowStepError when `name` is not part of the manifest.
   */
  async startStep(name: string, opts: StartStepOptions = {}): Promise<WorkflowStep> {
    if (this.wf.steps_manifest.length > 0 && !this.wf.steps_manifest.includes(name)) {
      throw new InvalidWorkflowStepError(
        `Step ${JSON.stringify(name)} is not declared in the workflow manifest ` +
          `${JSON.stringify(this.wf.steps_manifest)}.`,
      );
    }

    // v1.1 — resolve retry config: explicit opts > workflow-level
    // cached config > server defaults (omitted from the request body).
    const cached = this.retryConfig.get(name) ?? {};
    const maxAttempts = opts.maxAttempts ?? cached.maxAttempts;
    const retryPolicy = opts.retryPolicy ?? cached.retryPolicy;
    const retryInitial =
      opts.retryInitialDelaySeconds ?? cached.retryInitialDelaySeconds;
    const retryMax =
      opts.retryMaxDelaySeconds ?? cached.retryMaxDelaySeconds;
    const retryJitter = opts.retryJitter ?? cached.retryJitter;

    const body: Record<string, JsonValue> = { name };
    if (opts.input !== undefined) body.input = opts.input;
    if (opts.snapshotId !== undefined) body.snapshot_id = opts.snapshotId;
    if (opts.initialStatus !== undefined) body.initial_status = opts.initialStatus;
    // Only forward retry params that diverge from the v1.0 defaults so
    // the body stays compact for callers that haven't opted in.
    if (maxAttempts !== undefined && maxAttempts !== 1) {
      body.max_attempts = maxAttempts;
    }
    if (retryPolicy !== undefined && retryPolicy !== "none") {
      body.retry_policy = retryPolicy;
    }
    if (retryInitial !== undefined && retryInitial !== 1.0) {
      body.retry_initial_delay_seconds = retryInitial;
    }
    if (retryMax !== undefined && retryMax !== 60.0) {
      body.retry_max_delay_seconds = retryMax;
    }
    if (retryJitter !== undefined && retryJitter !== true) {
      body.retry_jitter = retryJitter;
    }

    const step = await this.http.requestJson<WorkflowStep>({
      method: "POST",
      path: this.stepsPath(),
      json: body,
    });
    this.recordStep(step);
    return step;
  }

  /**
   * Mark `stepId` completed.
   *
   * `snapshotId` is the canonical resume point — {@link resumeInfo}
   * surfaces it to the next agent.
   */
  async completeStep(
    stepId: string,
    opts: CompleteStepOptions = {},
  ): Promise<WorkflowStep> {
    return this.patchStep(stepId, {
      status: "completed",
      ...(opts.output !== undefined ? { output: opts.output } : {}),
      ...(opts.snapshotId !== undefined ? { snapshot_id: opts.snapshotId } : {}),
    });
  }

  /** Mark `stepId` failed with a free-text `error`. */
  async failStep(
    stepId: string,
    error: string,
    opts: { output?: JsonValue } = {},
  ): Promise<WorkflowStep> {
    return this.patchStep(stepId, {
      status: "failed",
      error,
      ...(opts.output !== undefined ? { output: opts.output } : {}),
    });
  }

  /** Mark `stepId` cancelled. */
  async cancelStep(stepId: string): Promise<WorkflowStep> {
    return this.patchStep(stepId, { status: "cancelled" });
  }

  // -- whole-workflow operations ---------------------------------------

  /**
   * Cancel the entire workflow on the server and refresh the cached model.
   */
  async cancel(): Promise<void> {
    const updated = await this.http.requestJson<Workflow>({
      method: "POST",
      path: `${this.workflowPath()}/cancel`,
    });
    this.wf = updated;
  }

  /**
   * Return the next pending step plus the snapshot to restore from.
   *
   * Crash → restart → call this → restore from `snapshot_id` → continue
   * at `next_step`.
   */
  async resumeInfo(): Promise<ResumeInfo> {
    return this.http.requestJson<ResumeInfo>({
      method: "GET",
      path: `${this.workflowPath()}/resume`,
    });
  }

  /** Re-fetch the full {@link Workflow} (with its step log) from the server. */
  async refresh(): Promise<void> {
    this.wf = await this.http.requestJson<Workflow>({
      method: "GET",
      path: this.workflowPath(),
    });
  }

  // -- v0.5: durable workflow executor (leases) ----------------------

  /**
   * Steps in `pending` status — ready for a worker to lease.
   *
   * The v0.2 in-process flow creates steps directly in `running`, so the
   * list is empty unless a worker is in the loop (or a driver has staged
   * steps in `initial_status="pending"`).
   */
  async pendingSteps(): Promise<WorkflowStep[]> {
    const res = await this.http.requestJson<{ steps: WorkflowStep[] }>({
      method: "GET",
      path: `${this.workflowPath()}/pending`,
    });
    return res.steps ?? [];
  }

  /** Leases past their expiry that haven't been reaped yet. */
  async expiredLeases(): Promise<Lease[]> {
    const res = await this.http.requestJson<{ leases: Lease[] }>({
      method: "GET",
      path: `${this.workflowPath()}/expired`,
    });
    return res.leases ?? [];
  }

  /**
   * Try to lease `stepId` for `workerId`.
   *
   * Returns the {@link Lease} on success, or `null` on a 409
   * `LEASE_CONFLICT` (someone else got it). Other errors propagate as
   * the corresponding {@link PlinthError} subclass.
   */
  async leaseStep(
    stepId: string,
    workerId: string,
    opts: { ttlSeconds?: number } = {},
  ): Promise<Lease | null> {
    try {
      return await this.http.requestJson<Lease>({
        method: "POST",
        path: `${this.stepsPath()}/${encodePath(stepId)}/lease`,
        json: {
          worker_id: workerId,
          ttl_seconds: opts.ttlSeconds ?? 60,
        },
      });
    } catch (err) {
      if (err instanceof LeaseConflictError) return null;
      throw err;
    }
  }

  /** Extend the lease on `stepId` (must be held by `workerId`). */
  async heartbeatStep(
    stepId: string,
    workerId: string,
    opts: { ttlSeconds?: number } = {},
  ): Promise<Lease> {
    const body: Record<string, JsonValue> = { worker_id: workerId };
    if (opts.ttlSeconds !== undefined) body.ttl_seconds = opts.ttlSeconds;
    return this.http.requestJson<Lease>({
      method: "POST",
      path: `${this.stepsPath()}/${encodePath(stepId)}/heartbeat`,
      json: body,
    });
  }

  /**
   * Release the lease on `stepId`, marking the step `status`.
   *
   * `status` is typically `completed` or `failed`. After the request the
   * cached workflow model is refreshed so subsequent reads reflect the
   * new step lifecycle.
   */
  async releaseStep(
    stepId: string,
    workerId: string,
    opts: {
      status: "completed" | "failed" | "cancelled" | "pending";
      output?: JsonValue;
      error?: string;
      snapshotId?: string;
    },
  ): Promise<Lease> {
    const body: Record<string, JsonValue> = {
      worker_id: workerId,
      status: opts.status,
    };
    if (opts.output !== undefined) body.output = opts.output;
    if (opts.error !== undefined) body.error = opts.error;
    if (opts.snapshotId !== undefined) body.snapshot_id = opts.snapshotId;
    const lease = await this.http.requestJson<Lease>({
      method: "POST",
      path: `${this.stepsPath()}/${encodePath(stepId)}/release`,
      json: body,
    });
    // Refresh cached steps so callers can read the new status.
    try {
      await this.refresh();
    } catch {
      // Best-effort: a refresh failure shouldn't mask a successful release.
    }
    return lease;
  }

  // -- v1.1: dead-letter queue ----------------------------------------

  /**
   * List every {@link DLQEntry} recorded for this workflow.
   *
   * Entries are returned newest-first by ``failed_at``. An empty array
   * means no step has yet exhausted its retries.
   */
  async dlq(): Promise<DLQEntry[]> {
    const res = await this.http.requestJson<{ entries: DLQEntry[] }>({
      method: "GET",
      path: `${this.workflowPath()}/dlq`,
    });
    return res.entries ?? [];
  }

  /**
   * Replay ``dlqId`` as a fresh attempt of the same step name.
   *
   * The server creates a new step row in ``pending`` status (so a
   * worker can immediately lease it) and deletes the DLQ entry in the
   * same transaction. Returns the new {@link WorkflowStep}.
   */
  async replayDlq(dlqId: string): Promise<WorkflowStep | null> {
    const res = await this.http.requestJson<DLQReplayResult>({
      method: "POST",
      path: `${this.workflowPath()}/dlq/${encodePath(dlqId)}/replay`,
    });
    // Refresh the cached workflow so the new step lands in the
    // handle's step log.
    try {
      await this.refresh();
    } catch {
      // Best-effort: a refresh failure shouldn't mask a successful replay.
    }
    return res.replayed_step;
  }

  /** Delete a DLQ entry without replaying it (operator dismissal). */
  async deleteDlq(dlqId: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: `${this.workflowPath()}/dlq/${encodePath(dlqId)}`,
    });
  }

  // -- internal --------------------------------------------------------

  private async patchStep(
    stepId: string,
    body: Record<string, JsonValue>,
  ): Promise<WorkflowStep> {
    const step = await this.http.requestJson<WorkflowStep>({
      method: "PATCH",
      path: `${this.stepsPath()}/${encodePath(stepId)}`,
      json: body,
    });
    this.recordStep(step);
    return step;
  }

  private recordStep(step: WorkflowStep): void {
    const existing = this.wf.steps;
    for (let i = 0; i < existing.length; i++) {
      if (existing[i]!.id === step.id) {
        existing[i] = step;
        return;
      }
    }
    existing.push(step);
  }

  private workflowPath(): string {
    return `/v1/workspaces/${encodePath(this.workspaceId)}/workflows/${encodePath(this.wf.id)}`;
  }

  private stepsPath(): string {
    return `${this.workflowPath()}/steps`;
  }
}

/**
 * Client for the v0.2 Workflows API on a workspace.
 *
 * Reachable via `ws.workflows`. Returns {@link WorkflowHandle} from every
 * non-`list` method so callers can chain step transitions in one
 * expression.
 */
export class WorkflowsClient {
  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
  ) {}

  /**
   * Create a new workflow with the given step manifest.
   *
   * v1.1: ``opts.steps`` may be a list of dicts carrying per-step retry
   * configuration. The server still receives a list of step names; the
   * retry config is cached on the returned handle so subsequent
   * {@link WorkflowHandle.startStep} calls forward it automatically.
   */
  async create(name: string, opts: WorkflowCreateOptions): Promise<WorkflowHandle> {
    // Normalise ``string | { name, ...retry }`` into a server-friendly
    // ``string[]`` plus a name → retry-config map for the handle.
    const stepNames: string[] = [];
    const retryConfig = new Map<string, StepRetryConfig>();
    for (const entry of opts.steps) {
      if (typeof entry === "string") {
        stepNames.push(entry);
        continue;
      }
      stepNames.push(entry.name);
      const cfg: StepRetryConfig = {};
      if (entry.maxAttempts !== undefined) cfg.maxAttempts = entry.maxAttempts;
      if (entry.retryPolicy !== undefined) cfg.retryPolicy = entry.retryPolicy;
      if (entry.retryInitialDelaySeconds !== undefined) {
        cfg.retryInitialDelaySeconds = entry.retryInitialDelaySeconds;
      }
      if (entry.retryMaxDelaySeconds !== undefined) {
        cfg.retryMaxDelaySeconds = entry.retryMaxDelaySeconds;
      }
      if (entry.retryJitter !== undefined) cfg.retryJitter = entry.retryJitter;
      if (Object.keys(cfg).length > 0) retryConfig.set(entry.name, cfg);
    }

    const body: Record<string, JsonValue> = {
      name,
      steps: stepNames,
    };
    if (opts.metadata !== undefined) body.metadata = opts.metadata;

    const wf = await this.http.requestJson<Workflow>({
      method: "POST",
      path: this.basePath(),
      json: body,
    });
    return new WorkflowHandle(this.http, this.workspaceId, wf, retryConfig);
  }

  /**
   * Idempotent create-by-name.
   *
   * If a workflow with `name` already exists, returns it; otherwise
   * creates one. Uses the bare list endpoint to look up the existing
   * workflow, then re-fetches via {@link get} so the cached model has
   * the full step log.
   */
  async getOrCreate(name: string, opts: WorkflowCreateOptions): Promise<WorkflowHandle> {
    const all = await this.list();
    const existing = all.find((w) => w.name === name);
    if (existing !== undefined) return this.get(existing.id);
    return this.create(name, opts);
  }

  /** Fetch a workflow by ID. */
  async get(workflowId: string): Promise<WorkflowHandle> {
    const wf = await this.http.requestJson<Workflow>({
      method: "GET",
      path: `${this.basePath()}/${encodePath(workflowId)}`,
    });
    return new WorkflowHandle(this.http, this.workspaceId, wf);
  }

  /**
   * List every workflow on the workspace as bare {@link Workflow} rows.
   *
   * Returns the raw model (one HTTP call) — to act on a workflow, follow
   * up with {@link get} for a {@link WorkflowHandle}.
   */
  async list(): Promise<Workflow[]> {
    const res = await this.http.requestJson<{ workflows: Workflow[] }>({
      method: "GET",
      path: this.basePath(),
    });
    return res.workflows ?? [];
  }

  private basePath(): string {
    return `/v1/workspaces/${encodePath(this.workspaceId)}/workflows`;
  }
}
