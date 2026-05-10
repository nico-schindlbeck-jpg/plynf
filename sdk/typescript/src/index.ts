/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Public entry point for `@plinth/sdk`.
 *
 * Re-exports the {@link Plinth} client, supporting classes, types,
 * and the typed error hierarchy. Internal modules (`http.ts`, etc.)
 * are intentionally not re-exported.
 */

export { Plinth } from "./client.js";
export type { AgentContext } from "./client.js";

export { Workspace, KVClient, FilesClient, LocksClient, SnapshotProxy } from "./workspace.js";
export { ToolsClient } from "./tools.js";
export type { InvokeOptions } from "./tools.js";

// v0.2 — channels & workflows
export { ChannelsClient } from "./channels.js";
export type {
  ChannelReceiveOptions,
  ChannelSendOptions,
  ChannelWaitOptions,
} from "./channels.js";

export { WorkflowsClient, WorkflowHandle } from "./workflows.js";
export type {
  CompleteStepOptions,
  StartStepOptions,
  StepRetryConfig,
  WorkflowCreateOptions,
  WorkflowStepSpec,
} from "./workflows.js";

// v0.5 — durable workflow workers
export { WorkersClient } from "./workers.js";

// v1.2 — LLM facade
export { LLMClient } from "./llm.js";
export type { LLMClientHost, RetryConfig as LLMRetryConfig } from "./llm.js";

export {
  AnthropicProvider,
  ANTHROPIC_PRICING,
  buildProvider as buildLLMProvider,
  MockProvider,
  MOCK_PRICING,
  OpenAIProvider,
  OPENAI_PRICING,
} from "./llm-providers/index.js";

export type {
  AnthropicProviderConfig,
  LLMProvider,
  LLMProviderRequest,
  MockProviderConfig,
  OpenAIProviderConfig,
} from "./llm-providers/index.js";

// v0.3 — identity
export { IdentityClient } from "./identity.js";
export type { IssueTokenOptions } from "./identity.js";

// v0.3 — tokens helpers
export {
  ENCODING_NAME,
  SONNET_INPUT_USD_PER_MTOK,
  SONNET_OUTPUT_USD_PER_MTOK,
  count as countTokens,
  estimateCost,
  heuristicCount,
} from "./tokens.js";

export {
  // Base
  PlinthError,
  // 400
  InvalidArgumentsError,
  InvalidWorkflowStepError,
  SchemaViolationError,
  // 401 / identity
  UnauthorizedError,
  InvalidTokenError,
  TokenExpiredError,
  TokenRevokedError,
  // 404
  WorkspaceNotFoundError,
  KeyNotFoundError,
  FileNotFoundError,
  SnapshotNotFoundError,
  BranchNotFoundError,
  ToolNotFoundError,
  ChannelNotFoundError,
  MessageNotFoundError,
  WorkflowNotFoundError,
  WorkflowStepNotFoundError,
  SigningKeyNotFoundError,
  // tool / rate
  ToolInvocationError,
  RateLimitedError,
  CostCapExceededError,
  // v0.6 — generic resource locks
  LockConflictError,
  LockNotFoundError,
  LockNotHeldError,
  // v0.5 — durable workflow executor (leases + workers)
  LeaseConflictError,
  LeaseNotHeldError,
  WorkerNotFoundError,
  NoHandlerError,
  // v1.2 — LLM layer
  LLMError,
  LLMProviderError,
  LLMProviderNotConfiguredError,
  LLMProviderNotInstalledError,
  LLMRateLimitedError,
  LLMRetryExhaustedError,
} from "./errors.js";

export type {
  // v0.1
  AuditEvent,
  AuditQuery,
  Branch,
  DiffResult,
  DryRunResponse,
  ErrorEnvelope,
  FileEntry,
  InvokeRequest,
  InvokeResponse,
  ISODateTime,
  JsonValue,
  KVEntry,
  MergeResult,
  PlinthConfig,
  Snapshot,
  Tool,
  ToolAuthMethod,
  ToolRegistration,
  ToolSideEffects,
  ToolTransport,
  Workspace as WorkspaceRecord,
  // v0.2
  AgentLimits,
  Channel,
  ChannelMessage,
  ChannelSchema,
  ChannelSchemaSetBody,
  ChannelSendBody,
  LimitsStatus,
  ReplayBatchResult,
  ReplayFailure,
  ResumeInfo,
  SchemaCheckFailure,
  SchemaCheckResult,
  SchemaValidationError,
  Workflow,
  WorkflowRetryPolicy,
  WorkflowStatus,
  WorkflowStep,
  // v1.1 — workflow retries + DLQ
  DLQEntry,
  DLQReplayResult,
  // v0.3
  TokenClaims,
  TokenInfo,
  TokenIssueRequest,
  TokenIssueResponse,
  // v0.4
  SigningKey,
  // v0.6
  Lock,
  LockAcquireOptions,
  LockHeartbeatOptions,
  LockReleaseOptions,
  WithLockOptions,
  // v0.5 — durable workflow executor (leases + workers)
  Lease,
  LeaseStatus,
  WorkerRecord,
  WorkerRegistration,
  WorkerStatus,
  // v1.2 — LLM types
  LLMMessage,
  LLMProviderConfig,
  LLMProviderName,
  LLMRequest,
  LLMResponse,
  LLMRole,
  LLMStreamChunk,
} from "./types.js";
