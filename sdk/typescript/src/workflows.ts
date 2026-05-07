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

import { InvalidWorkflowStepError } from "./errors.js";
import { encodePath, type HttpClient } from "./http.js";
import type {
  JsonValue,
  ResumeInfo,
  Workflow,
  WorkflowStatus,
  WorkflowStep,
} from "./types.js";

/** Options accepted by {@link WorkflowsClient.create} / `getOrCreate`. */
export interface WorkflowCreateOptions {
  /** Ordered list of expected step names — the manifest. */
  steps: string[];
  /** Optional free-form metadata (topic, parent run ID, etc.). */
  metadata?: Record<string, JsonValue>;
}

/** Options accepted by {@link WorkflowHandle.startStep}. */
export interface StartStepOptions {
  /** Optional input payload to record on the step. */
  input?: JsonValue;
  /** Optional snapshot taken before the step ran. */
  snapshotId?: string;
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

  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
    model: Workflow,
  ) {
    this.wf = model;
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

    const body: Record<string, JsonValue> = { name };
    if (opts.input !== undefined) body.input = opts.input;
    if (opts.snapshotId !== undefined) body.snapshot_id = opts.snapshotId;

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

  /** Create a new workflow with the given step manifest. */
  async create(name: string, opts: WorkflowCreateOptions): Promise<WorkflowHandle> {
    const body: Record<string, JsonValue> = {
      name,
      steps: [...opts.steps],
    };
    if (opts.metadata !== undefined) body.metadata = opts.metadata;

    const wf = await this.http.requestJson<Workflow>({
      method: "POST",
      path: this.basePath(),
      json: body,
    });
    return new WorkflowHandle(this.http, this.workspaceId, wf);
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
