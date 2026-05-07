/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Tool gateway client.
 *
 * Wraps the gateway service at `/v1/invoke`, `/v1/tools/*`, and
 * `/v1/audit/*`. The gateway transparently caches/audits every call,
 * so consumers don't need to reach for cache primitives directly.
 */

import type { HttpClient } from "./http.js";
import type {
  AuditEvent,
  AuditQuery,
  DryRunResponse,
  InvokeRequest,
  InvokeResponse,
  JsonValue,
  Tool,
  ToolRegistration,
} from "./types.js";

/** Optional metadata attached to an invocation for audit attribution. */
export interface InvokeOptions {
  /** Workspace this call is associated with — propagated to audit log. */
  workspaceId?: string;
  /** Agent identifier — propagated to audit log. */
  agentId?: string;
  /** Disable result caching for this call (default: cache enabled). */
  cache?: boolean;
  /** Dedup key for at-least-once retry semantics. */
  idempotencyKey?: string;
}

/**
 * Client for the gateway's `/v1/tools` and `/v1/invoke` endpoints.
 *
 * Construct via {@link Plinth.tools} — never directly.
 */
export class ToolsClient {
  constructor(private readonly http: HttpClient) {}

  /**
   * Invoke a registered tool through the gateway.
   *
   * The gateway caches based on `(tool_id, arguments)` when the tool
   * declares `cache_ttl_seconds` and the call sets `cache: true`
   * (the default). The returned `cached` flag tells you which path you hit.
   */
  async invoke(
    toolId: string,
    args: Record<string, JsonValue>,
    opts: InvokeOptions = {},
  ): Promise<InvokeResponse> {
    const body: InvokeRequest = {
      tool_id: toolId,
      arguments: args,
      ...(opts.workspaceId !== undefined ? { workspace_id: opts.workspaceId } : {}),
      ...(opts.agentId !== undefined ? { agent_id: opts.agentId } : {}),
      ...(opts.cache !== undefined ? { cache: opts.cache } : {}),
      ...(opts.idempotencyKey !== undefined ? { idempotency_key: opts.idempotencyKey } : {}),
    };
    return this.http.requestJson<InvokeResponse>({
      method: "POST",
      path: "/v1/invoke",
      json: body as unknown as JsonValue,
    });
  }

  /**
   * Dry-run an invocation — returns what would happen without actually
   * calling the underlying tool. Useful for cost/latency budgeting.
   */
  async dryRun(
    toolId: string,
    args: Record<string, JsonValue>,
    opts: InvokeOptions = {},
  ): Promise<DryRunResponse> {
    const body: InvokeRequest = {
      tool_id: toolId,
      arguments: args,
      ...(opts.workspaceId !== undefined ? { workspace_id: opts.workspaceId } : {}),
      ...(opts.agentId !== undefined ? { agent_id: opts.agentId } : {}),
      ...(opts.cache !== undefined ? { cache: opts.cache } : {}),
      ...(opts.idempotencyKey !== undefined ? { idempotency_key: opts.idempotencyKey } : {}),
    };
    return this.http.requestJson<DryRunResponse>({
      method: "POST",
      path: "/v1/invoke/dry-run",
      json: body as unknown as JsonValue,
    });
  }

  /** Register a new tool with the gateway. */
  async register(registration: ToolRegistration): Promise<Tool> {
    return this.http.requestJson<Tool>({
      method: "POST",
      path: "/v1/tools/register",
      json: registration as unknown as JsonValue,
    });
  }

  /** List all registered tools. */
  async list(): Promise<Tool[]> {
    const res = await this.http.requestJson<{ tools: Tool[] }>({
      method: "GET",
      path: "/v1/tools",
    });
    return res.tools;
  }

  /** Fetch a single registered tool by ID. */
  async get(toolId: string): Promise<Tool> {
    return this.http.requestJson<Tool>({
      method: "GET",
      path: `/v1/tools/${encodeURIComponent(toolId)}`,
    });
  }

  /** Unregister a tool. */
  async deregister(toolId: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: `/v1/tools/${encodeURIComponent(toolId)}`,
    });
  }

  /** Query the gateway audit log. */
  async audit(query: AuditQuery = {}): Promise<AuditEvent[]> {
    const res = await this.http.requestJson<{ events: AuditEvent[] }>({
      method: "GET",
      path: "/v1/audit",
      query: {
        workspace_id: query.workspaceId,
        tool_id: query.toolId,
        since: query.since,
        limit: query.limit,
      },
    });
    return res.events;
  }
}
