/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * TypeScript type definitions mirroring the Pydantic models in CONTRACTS.md.
 *
 * These types are deliberately structural rather than nominal — runtime
 * validation (e.g. via zod) can be layered on later without changing the
 * shape consumed by client code.
 */

/** ISO-8601 timestamp string as returned by the FastAPI services. */
export type ISODateTime = string;

/** Arbitrary JSON-serializable value. */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** Top-level isolation boundary for an agent's state. */
export interface Workspace {
  id: string;
  name: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  metadata: Record<string, JsonValue>;
}

/** Versioned key-value entry. */
export interface KVEntry {
  workspace_id: string;
  key: string;
  value: JsonValue;
  version: number;
  created_at: ISODateTime;
  deleted: boolean;
  branch_id: string | null;
}

/** Versioned file metadata (content is fetched separately as raw bytes). */
export interface FileEntry {
  workspace_id: string;
  path: string;
  size: number;
  sha256: string;
  content_type: string;
  version: number;
  created_at: ISODateTime;
  deleted: boolean;
  branch_id: string | null;
}

/** Immutable point-in-time capture of every key/file in a workspace. */
export interface Snapshot {
  id: string;
  workspace_id: string;
  name: string;
  message: string | null;
  created_at: ISODateTime;
  kv_versions: Record<string, number>;
  file_versions: Record<string, number>;
  parent_snapshot_id: string | null;
}

/** Divergent timeline anchored to a snapshot. */
export interface Branch {
  id: string;
  workspace_id: string;
  name: string;
  from_snapshot_id: string;
  created_at: ISODateTime;
  merged: boolean;
  merged_at: ISODateTime | null;
}

/** Diff between two snapshots. */
export interface DiffResult {
  kv_added: string[];
  kv_modified: string[];
  kv_deleted: string[];
  files_added: string[];
  files_modified: string[];
  files_deleted: string[];
}

/** Result of merging a branch back into main. */
export interface MergeResult {
  branch_id: string;
  workspace_id?: string;
  merged_at: ISODateTime;
  /** Latest server shape includes per-resource lists, but we keep `merged`
   * for backward-compatibility with prior client code. */
  merged?: boolean;
  kv_keys_merged?: string[];
  file_paths_merged?: string[];
  conflicts: string[];
}

// --- Gateway / tools ---

export type ToolTransport = "http" | "stdio";
export type ToolSideEffects = "none" | "read" | "write";
export type ToolAuthMethod = "none" | "bearer" | "oauth2";

/** Body sent to `POST /v1/tools/register`. */
export interface ToolRegistration {
  tool_id: string;
  name: string;
  description: string;
  transport: ToolTransport;
  endpoint: string;
  input_schema: Record<string, JsonValue>;
  output_schema: Record<string, JsonValue>;
  idempotent?: boolean;
  side_effects?: ToolSideEffects;
  cache_ttl_seconds?: number | null;
  auth_method?: ToolAuthMethod;
  auth_config?: Record<string, JsonValue>;
}

/** Registered tool as returned by the gateway. */
export interface Tool extends ToolRegistration {
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface InvokeRequest {
  tool_id: string;
  arguments: Record<string, JsonValue>;
  workspace_id?: string | null;
  agent_id?: string | null;
  cache?: boolean;
  idempotency_key?: string | null;
}

export interface InvokeResponse {
  tool_id: string;
  arguments: Record<string, JsonValue>;
  // MCP responses are intentionally untyped — schemas vary per tool.
  result: unknown;
  cached: boolean;
  duration_ms: number;
  audit_id: string;
  cost_estimate_usd: number;
}

export interface DryRunResponse {
  tool_id: string;
  arguments: Record<string, JsonValue>;
  would_invoke: boolean;
  cached_result: unknown;
  estimated_cost_usd: number;
  estimated_duration_ms: number;
}

export interface AuditEvent {
  id: string;
  timestamp: ISODateTime;
  tool_id: string;
  workspace_id: string | null;
  agent_id: string | null;
  arguments_hash: string;
  result_hash: string;
  cached: boolean;
  duration_ms: number;
  cost_estimate_usd: number;
  error: string | null;
}

export interface AuditQuery {
  workspaceId?: string;
  toolId?: string;
  /** Relative window (e.g. "1h", "24h") or ISO-8601 timestamp. */
  since?: string;
  limit?: number;
}

// --- v0.2: Channels ---

/** A single message persisted on a workspace channel. */
export interface ChannelMessage {
  id: string;
  channel: string;
  workspace_id: string;
  seq: number;
  payload: JsonValue;
  sender: string | null;
  type: string | null;
  correlation_id: string | null;
  headers: Record<string, string>;
  sent_at: ISODateTime;
  delivered_at: ISODateTime | null;
}

/** Metadata about a workspace channel. */
export interface Channel {
  name: string;
  workspace_id: string;
  message_count: number;
  created_at: ISODateTime;
  last_send_at: ISODateTime | null;
  last_receive_at: ISODateTime | null;
}

/** Body sent to `POST /v1/workspaces/{ws}/channels/{name}/send`. */
export interface ChannelSendBody {
  payload: JsonValue;
  sender?: string;
  type?: string;
  correlation_id?: string;
  headers?: Record<string, string>;
}

/**
 * v0.5 — JSON Schema attached to a workspace channel.
 *
 * Channels carrying a schema validate every payload at send time. Failed
 * validations land on a hidden `<channel>.deadletter` sub-channel and the
 * caller receives a 422 `SCHEMA_VIOLATION`. The `version` field
 * auto-increments on each `setSchema` call so consumers can detect when a
 * DLQ message was queued under a different schema.
 */
export interface ChannelSchema {
  workspace_id: string;
  channel_name: string;
  schema_json: Record<string, JsonValue>;
  version: number;
  updated_at: ISODateTime;
}

/** Body sent to `POST /v1/workspaces/{ws}/channels/{name}/schema` (v0.5). */
export interface ChannelSchemaSetBody {
  schema: Record<string, JsonValue>;
}

/**
 * v0.5 — one validation failure entry produced by the JSON Schema validator.
 *
 * Surfaces both the user-facing `message` and the structural `path` so a
 * UI can highlight the offending field. `validator` is best-effort and may
 * be the empty string for older servers.
 */
export interface SchemaValidationError {
  message: string;
  path: Array<string | number>;
  validator?: string;
}

/** v0.6 — one ``check`` failure sample: a message id + its validation errors. */
export interface SchemaCheckFailure {
  msg_id: string;
  errors: SchemaValidationError[];
}

/**
 * v0.6 — outcome of ``ChannelsClient.checkSchema``.
 *
 * `sample_failures` is bounded server-side to 10 entries so a runaway
 * `invalid` count never produces a multi-megabyte response. Counts in
 * `checked` / `valid` / `invalid` remain accurate even when the sample
 * list is truncated.
 */
export interface SchemaCheckResult {
  channel: string;
  scope: "main" | "deadletter" | "both";
  checked: number;
  valid: number;
  invalid: number;
  sample_failures: SchemaCheckFailure[];
}

/** v0.6 — one ``replay-all`` failure: a message id + a human-readable reason. */
export interface ReplayFailure {
  msg_id: string;
  reason: string;
}

/**
 * v0.6 — outcome of ``ChannelsClient.replayAllDlq``.
 *
 * `failures` is bounded server-side to 50 entries; `attempted` /
 * `succeeded` / `failed` are accurate even when the list is truncated.
 * `dry_run` is echoed so callers can distinguish "would succeed" from
 * "did succeed" results without re-checking their own request.
 */
export interface ReplayBatchResult {
  channel: string;
  attempted: number;
  succeeded: number;
  failed: number;
  failures: ReplayFailure[];
  dry_run: boolean;
}

// --- v0.2: Workflows ---

/** One of the lifecycle states a workflow / step can be in. */
export type WorkflowStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** A single step in a workflow's log. */
export interface WorkflowStep {
  id: string;
  workflow_id: string;
  name: string;
  status: WorkflowStatus;
  attempt: number;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  input: JsonValue;
  output: JsonValue;
  error: string | null;
  snapshot_id: string | null;
  created_at: ISODateTime | null;
}

/** A workflow: a manifest of expected steps + a log of completed ones. */
export interface Workflow {
  id: string;
  workspace_id: string;
  name: string;
  steps_manifest: string[];
  steps: WorkflowStep[];
  status: WorkflowStatus;
  metadata: Record<string, JsonValue>;
  created_at: ISODateTime;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
}

/** Information returned by `GET /workflows/{id}/resume`. */
export interface ResumeInfo {
  workflow_id: string;
  workflow_status: WorkflowStatus;
  next_step: string | null;
  last_completed: WorkflowStep | null;
  snapshot_id: string | null;
}

// --- v0.5: Durable workflow executor (leases + workers) ---

/** Lifecycle for a workflow-step lease held by a worker. */
export type LeaseStatus = "running" | "released" | "expired";

/**
 * A soft-lock held by a worker over a single workflow step.
 *
 * The reaper sweeps leases past their `expires_at` and marks them
 * `expired` so another worker can claim the step.
 */
export interface Lease {
  step_id: string;
  worker_id: string;
  acquired_at: ISODateTime;
  expires_at: ISODateTime;
  heartbeat_at: ISODateTime;
  status: LeaseStatus;
}

/** Lifecycle for a worker process. */
export type WorkerStatus = "active" | "draining" | "gone";

/**
 * A registered worker process.
 *
 * The `id` is server-assigned at registration. The lease reaper sweeps
 * `active` → `gone` on heartbeat lapse; `draining` is set by the worker
 * itself when it shuts down gracefully.
 */
export interface WorkerRecord {
  id: string;
  hostname: string | null;
  pid: number | null;
  started_at: ISODateTime;
  last_heartbeat_at: ISODateTime;
  status: WorkerStatus;
}

/** Body sent to `POST /v1/workers/register`. */
export interface WorkerRegistration {
  hostname?: string | null;
  pid?: number | null;
}

// --- v0.2: rate limits / cost caps (gateway service) ---

export interface AgentLimits {
  agent_id: string;
  rpm: number;
  burst: number;
  cost_cap_usd_hour: number;
  cost_cap_usd_day: number;
  updated_at: ISODateTime;
}

export interface LimitsStatus {
  agent_id: string;
  rpm_limit: number;
  rpm_used_in_window: number;
  cost_cap_usd_hour: number;
  cost_used_usd_hour: number;
  cost_cap_usd_day: number;
  cost_used_usd_day: number;
}

// --- v0.3: Identity ---

/** Body sent to `POST /v1/tokens` to mint a new capability token. */
export interface TokenIssueRequest {
  agent_id: string;
  tenant_id?: string;
  scopes: string[];
  workspace_id?: string | null;
  ttl_seconds?: number;
  metadata?: Record<string, JsonValue>;
}

/** Decoded JWT claims for a Plinth capability token. */
export interface TokenClaims {
  sub: string;
  iss: string;
  aud: string;
  iat: number;
  exp: number;
  jti: string;
  agent_id: string;
  tenant_id: string;
  workspace_id: string | null;
  scopes: string[];
  rate_limit?: Record<string, JsonValue> | null;
}

/** Response from `POST /v1/tokens`: the encoded JWT plus its decoded claims. */
export interface TokenIssueResponse {
  token: string;
  jti: string;
  expires_at: ISODateTime;
  claims: TokenClaims;
}

/** Public-safe view of a token (no secret). */
export interface TokenInfo {
  jti: string;
  agent_id: string;
  tenant_id: string;
  issued_at: ISODateTime;
  expires_at: ISODateTime;
  revoked: boolean;
  revoked_at: ISODateTime | null;
  metadata: Record<string, JsonValue>;
}

// --- v0.6: Generic resource locks ---

/**
 * A generic distributed lock over a named workspace resource.
 *
 * Locks are independent of the workflow-step lease primitive — they
 * exist so two agents can coordinate access to any named object
 * (KV key, file path, external resource handle) without each one having
 * to invent its own protocol.
 */
export interface Lock {
  name: string;
  workspace_id: string;
  holder: string;
  acquired_at: ISODateTime;
  expires_at: ISODateTime;
  heartbeat_at: ISODateTime;
  waiters: number;
}

/** Options accepted by `LocksClient.acquire`. */
export interface LockAcquireOptions {
  holder: string;
  ttlSeconds?: number;
  /** ``0`` is fail-fast; positive values poll up to this budget. */
  waitMs?: number;
}

/** Options accepted by `LocksClient.heartbeat`. */
export interface LockHeartbeatOptions {
  holder: string;
  ttlSeconds?: number;
}

/** Options accepted by `LocksClient.release`. */
export interface LockReleaseOptions {
  holder: string;
}

/** Options accepted by `LocksClient.withLock`. */
export interface WithLockOptions {
  ttlSeconds?: number;
  waitMs?: number;
  /** Heartbeat cadence in ms. ``0`` disables auto-heartbeats. */
  heartbeatIntervalMs?: number;
}

// --- v1.0: Per-tenant resource quotas ---

/** Quota envelope for a single tenant — mirrors `GET /v1/tenants/{id}/quotas`. */
export interface TenantQuotas {
  tenant_id: string;
  max_workspaces: number;
  max_storage_gb: number;
  max_channels_per_workspace: number;
  max_workflows_per_workspace: number;
  max_active_tokens: number;
  max_oauth_connections: number;
  max_cost_usd_day: number;
  max_cost_usd_month: number;
  max_invocations_per_minute: number;
  updated_at?: ISODateTime;
}

/**
 * Partial-update body for `POST /v1/tenants/{id}/quotas`.
 *
 * All fields optional — unset values fall back to the existing row, or
 * the contract defaults if no row exists.
 */
export interface TenantQuotasUpdate {
  max_workspaces?: number;
  max_storage_gb?: number;
  max_channels_per_workspace?: number;
  max_workflows_per_workspace?: number;
  max_active_tokens?: number;
  max_oauth_connections?: number;
  max_cost_usd_day?: number;
  max_cost_usd_month?: number;
  max_invocations_per_minute?: number;
}

/** Computed usage rollup from `GET /v1/tenants/{id}/usage`. */
export interface TenantUsage {
  tenant_id: string;
  workspaces: number;
  storage_gb: number;
  active_tokens: number;
  oauth_connections: number;
  cost_usd_day: number;
  cost_usd_month: number;
  last_invocation_at: ISODateTime | null;
  notes: Record<string, string>;
}

/**
 * Result of {@link ChannelsClient.previewSchemaChange}.
 *
 * Wraps two {@link SchemaCheckResult}s (main + DLQ) plus a recommendation
 * string suitable for direct UI display.
 */
export interface SchemaChangePreview {
  compatible: boolean;
  main_check: SchemaCheckResult;
  deadletter_check: SchemaCheckResult;
  recommendation: string;
}

// --- v0.4: Identity signing keys ---

/** Public-safe view of an RS256 signing key (never carries private material). */
export interface SigningKey {
  kid: string;
  alg: string;
  public_key_pem: string;
  created_at: ISODateTime;
  rotated_in_at: ISODateTime | null;
  expires_at: ISODateTime;
  active: boolean;
}

// --- Client config ---

export interface PlinthConfig {
  workspaceUrl?: string;
  gatewayUrl?: string;
  /** v0.3 — base URL for the identity service (token issue/verify/revoke). */
  identityUrl?: string;
  apiKey: string;
  /** Per-request timeout. Defaults to 30000ms. */
  timeoutMs?: number;
  /** Override the global fetch implementation (useful for testing). */
  fetch?: typeof fetch;
  /**
   * v1.0 — multi-region. The region id of the primary deployment this
   * client is talking to (e.g. `"eu-west-1"`). Surfaced in failover
   * logs and used as a lookup key when a server's 409 redirect points
   * at our own primary region.
   */
  region?: string;
  /**
   * v1.0 — multi-region. Ordered list of fallback region ids tried
   * after the primary on connection errors / 5xx / replica redirects.
   * Order matters; the SDK tries entries in the order listed here.
   */
  fallbackRegions?: ReadonlyArray<string>;
  /** Per-region workspace URL map: `{ region_id: url }`. */
  fallbackWorkspaceUrls?: Record<string, string>;
  /** Per-region gateway URL map: `{ region_id: url }`. */
  fallbackGatewayUrls?: Record<string, string>;
  /** Per-region identity URL map: `{ region_id: url }`. */
  fallbackIdentityUrls?: Record<string, string>;
}

/** Shape of the `{ "error": {...} }` envelope returned by every service. */
export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: Record<string, JsonValue>;
  };
}

