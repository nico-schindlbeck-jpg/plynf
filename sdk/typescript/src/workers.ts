/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Worker registration client for the durable workflow executor (v0.5).
 *
 * Mirrors the workspace service's `/v1/workers` endpoints. Reachable via
 * `client.workers` and used by the worker harness (`@plinth/workflow-worker`)
 * and ops tooling — application code rarely registers a worker directly.
 */

import { hostname as osHostname } from "node:os";
import { pid as processPid } from "node:process";

import type { HttpClient, QueryValue } from "./http.js";
import type { WorkerRecord, WorkerRegistration } from "./types.js";

/** Workspace-service `/v1/workers` client. */
export class WorkersClient {
  constructor(private readonly http: HttpClient) {}

  /**
   * Register a new worker process.
   *
   * `hostname` and `pid` default to the current Node process's values so
   * a typical worker just calls `client.workers.register()`.
   */
  async register(opts: WorkerRegistration = {}): Promise<WorkerRecord> {
    const body: Record<string, string | number | null> = {};
    body.hostname = opts.hostname !== undefined ? opts.hostname : safeHostname();
    body.pid = opts.pid !== undefined ? opts.pid : processPid;
    return this.http.requestJson<WorkerRecord>({
      method: "POST",
      path: "/v1/workers/register",
      json: body,
    });
  }

  /** Bump `last_heartbeat_at` for `workerId`. */
  async heartbeat(workerId: string): Promise<WorkerRecord> {
    return this.http.requestJson<WorkerRecord>({
      method: "POST",
      path: `/v1/workers/${encodeURIComponent(workerId)}/heartbeat`,
    });
  }

  /** Mark `workerId` as `draining` (graceful shutdown signal). */
  async drain(workerId: string): Promise<WorkerRecord> {
    return this.http.requestJson<WorkerRecord>({
      method: "POST",
      path: `/v1/workers/${encodeURIComponent(workerId)}/drain`,
    });
  }

  /** List registered workers (optionally filtered by `status`). */
  async list(opts: { status?: string } = {}): Promise<WorkerRecord[]> {
    const query: Record<string, QueryValue> = {};
    if (opts.status !== undefined) query.status = opts.status;
    const res = await this.http.requestJson<{ workers: WorkerRecord[] }>({
      method: "GET",
      path: "/v1/workers",
      query,
    });
    return res.workers ?? [];
  }
}

function safeHostname(): string {
  // ``os.hostname()`` may throw under certain sandbox conditions; the
  // workspace service tolerates ``""`` so we degrade rather than fail
  // the whole worker boot.
  try {
    return osHostname();
  } catch {
    return "";
  }
}
