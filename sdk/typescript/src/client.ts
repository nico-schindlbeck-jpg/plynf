/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Main entry point for the Plinth TypeScript SDK.
 *
 * Construct one {@link Plinth} per process; it owns one HTTP client per
 * service (workspace + gateway, plus identity in v0.3) and exposes
 * lazily-constructed handles to workspaces, tools, and identity.
 */

import { HttpClient } from "./http.js";
import { IdentityClient } from "./identity.js";
import { count as countTokensImpl, estimateCost as estimateCostImpl } from "./tokens.js";
import { ToolsClient } from "./tools.js";
import type { JsonValue, PlinthConfig, Workspace as WorkspaceModel } from "./types.js";
import { Workspace } from "./workspace.js";

const DEFAULT_WORKSPACE_URL = "http://localhost:7421";
const DEFAULT_GATEWAY_URL = "http://localhost:7422";
const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * The top-level SDK client.
 *
 * @example
 * ```ts
 * const client = new Plinth({
 *   workspaceUrl: "http://localhost:7421",
 *   gatewayUrl:   "http://localhost:7422",
 *   identityUrl:  "http://localhost:7425",   // v0.3, optional
 *   apiKey:       "local-dev",
 * });
 *
 * const ws = await client.workspace("research-task-1");
 * await ws.kv.set("topic", "renewable energy");
 * ```
 */
export class Plinth {
  /** Tool gateway client. */
  readonly tools: ToolsClient;

  /**
   * v0.3 identity client.
   *
   * Throws on access when `identityUrl` was not configured — callers who
   * never wired identity get a clear "you need to pass identityUrl"
   * error instead of a silent network failure.
   */
  get identity(): IdentityClient {
    if (!this.identityClient) {
      throw new Error(
        "Plinth.identity: identityUrl was not configured. Pass `identityUrl` " +
          "in the Plinth() options to enable token issuance/verification.",
      );
    }
    return this.identityClient;
  }

  private readonly workspaceHttp: HttpClient;
  private readonly gatewayHttp: HttpClient;
  private readonly identityHttp: HttpClient | null;
  private readonly identityClient: IdentityClient | null;

  constructor(config: PlinthConfig) {
    if (!config.apiKey) throw new TypeError("Plinth: apiKey is required");

    const workspaceUrl = config.workspaceUrl ?? DEFAULT_WORKSPACE_URL;
    const gatewayUrl = config.gatewayUrl ?? DEFAULT_GATEWAY_URL;

    if (!workspaceUrl) throw new TypeError("Plinth: workspaceUrl is required");
    if (!gatewayUrl) throw new TypeError("Plinth: gatewayUrl is required");

    const fetchImpl = config.fetch ?? globalThis.fetch;
    if (typeof fetchImpl !== "function") {
      throw new TypeError(
        "Plinth: no fetch implementation available. Pass `fetch` in config or run on Node 20+.",
      );
    }

    const defaultTimeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;

    this.workspaceHttp = new HttpClient({
      baseUrl: workspaceUrl,
      apiKey: config.apiKey,
      defaultTimeoutMs,
      fetch: fetchImpl,
    });
    this.gatewayHttp = new HttpClient({
      baseUrl: gatewayUrl,
      apiKey: config.apiKey,
      defaultTimeoutMs,
      fetch: fetchImpl,
    });
    this.tools = new ToolsClient(this.gatewayHttp);

    if (config.identityUrl) {
      this.identityHttp = new HttpClient({
        baseUrl: config.identityUrl,
        apiKey: config.apiKey,
        defaultTimeoutMs,
        fetch: fetchImpl,
      });
      this.identityClient = new IdentityClient(this.identityHttp);
    } else {
      this.identityHttp = null;
      this.identityClient = null;
    }
  }

  /**
   * Get-or-create a workspace by name.
   *
   * The lookup is done by listing workspaces and matching on `name`. If
   * none exists, one is created. Equivalent to the Python SDK's
   * `client.workspace(name)`.
   */
  async workspace(
    name: string,
    opts: { metadata?: Record<string, JsonValue> } = {},
  ): Promise<Workspace> {
    const existing = await this.findWorkspaceByName(name);
    if (existing) return new Workspace(this.workspaceHttp, existing);

    const created = await this.workspaceHttp.requestJson<WorkspaceModel>({
      method: "POST",
      path: "/v1/workspaces",
      json: {
        name,
        ...(opts.metadata !== undefined ? { metadata: opts.metadata } : {}),
      },
    });
    return new Workspace(this.workspaceHttp, created);
  }

  /** Fetch a workspace by stable ID (skips the get-or-create dance). */
  async getWorkspace(workspaceId: string): Promise<Workspace> {
    const record = await this.workspaceHttp.requestJson<WorkspaceModel>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(workspaceId)}`,
    });
    return new Workspace(this.workspaceHttp, record);
  }

  /** List all workspaces visible to this API key. */
  async listWorkspaces(): Promise<WorkspaceModel[]> {
    const res = await this.workspaceHttp.requestJson<{ workspaces: WorkspaceModel[] }>({
      method: "GET",
      path: "/v1/workspaces",
    });
    return res.workspaces;
  }

  /** Permanently delete a workspace and all of its versioned data. */
  async deleteWorkspace(workspaceId: string): Promise<void> {
    await this.workspaceHttp.requestVoid({
      method: "DELETE",
      path: `/v1/workspaces/${encodeURIComponent(workspaceId)}`,
    });
  }

  // -- Token counting (best-effort, offline) ---------------------------

  /**
   * Count tokens in `text` using the `cl100k_base` BPE.
   *
   * Falls back to a `words × 1.3` heuristic if the `gpt-tokenizer`
   * runtime dep is missing — the result is always a number, never an
   * exception. See {@link tokens} for the underlying implementation.
   */
  async countTokens(text: string): Promise<number> {
    return countTokensImpl(text);
  }

  /**
   * Estimate the USD cost of a Sonnet request given its token usage.
   *
   * Uses {@link tokens.SONNET_INPUT_USD_PER_MTOK} /
   * {@link tokens.SONNET_OUTPUT_USD_PER_MTOK}. Pass `0` for
   * `completionTokens` to estimate prompt-only cost.
   */
  estimateCost(promptTokens: number, completionTokens = 0): number {
    return estimateCostImpl(promptTokens, completionTokens);
  }

  // -- Agent helper ----------------------------------------------------

  /**
   * Run `fn` with a workspace-scoped context.
   *
   * The Python SDK's `@client.agent` decorator does not translate cleanly
   * to TypeScript; this method is the idiomatic equivalent. It pre-binds
   * a workspace and the tool gateway into a {@link AgentContext} so the
   * callback doesn't have to thread them through.
   */
  async withAgent<T>(
    name: string,
    workspaceName: string,
    fn: (ctx: AgentContext) => Promise<T> | T,
  ): Promise<T> {
    const workspace = await this.workspace(workspaceName);
    const ctx: AgentContext = { agentId: name, workspace, tools: this.tools };
    return await fn(ctx);
  }

  // --- Internals ------------------------------------------------------

  private async findWorkspaceByName(name: string): Promise<WorkspaceModel | null> {
    const all = await this.listWorkspaces();
    return all.find((w) => w.name === name) ?? null;
  }
}

/** Context object passed to the {@link Plinth.withAgent} callback. */
export interface AgentContext {
  agentId: string;
  workspace: Workspace;
  tools: ToolsClient;
}
