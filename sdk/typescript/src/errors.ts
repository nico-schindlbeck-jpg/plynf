/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Typed error hierarchy mirroring the codes in CONTRACTS.md.
 *
 * Every error from a Plinth service lands as either a subclass of
 * {@link PlinthError} or — for unrecognised codes — as the base class
 * itself. Consumers are encouraged to `instanceof`-check the specific
 * subclasses they care about and let everything else propagate.
 */

import type { ErrorEnvelope } from "./types.js";

/** Base error for everything thrown by the Plinth SDK. */
export class PlinthError extends Error {
  /** Stable string code from the service (e.g. `WORKSPACE_NOT_FOUND`). */
  readonly code: string;
  /** HTTP status if the error originated from a service response. */
  readonly status: number | undefined;
  /** Optional structured detail payload from the service. */
  readonly details: Record<string, unknown> | undefined;

  constructor(
    message: string,
    code = "INTERNAL_ERROR",
    status?: number,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "PlinthError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

// ---------------------------------------------------------------------------
// 400 — validation
// ---------------------------------------------------------------------------

export class InvalidArgumentsError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "INVALID_ARGUMENTS", status, details);
    this.name = "InvalidArgumentsError";
  }
}

/**
 * v0.5 — channel send failed JSON-Schema validation.
 *
 * Subclasses {@link InvalidArgumentsError} so existing catch blocks for
 * 400-class validation errors automatically pick this up. Surfaces the
 * structured validator errors and the ID of the message the server filed
 * in the dead-letter queue.
 */
export class SchemaViolationError extends InvalidArgumentsError {
  /** List of validator errors. Always non-empty when raised. */
  readonly errors: Array<{ message: string; path: Array<string | number>; validator?: string }>;
  /** ID of the message persisted to the DLQ; `null` if the server didn't surface one. */
  readonly deadletterMsgId: string | null;
  /** The channel the send was directed at, when known. */
  readonly channel: string | null;

  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, status, details);
    Object.defineProperty(this, "code", { value: "SCHEMA_VIOLATION", enumerable: true });
    this.name = "SchemaViolationError";

    const rawErrors = (details && Array.isArray((details as { errors?: unknown }).errors))
      ? ((details as { errors: unknown[] }).errors)
      : [];
    const collected: Array<{
      message: string;
      path: Array<string | number>;
      validator?: string;
    }> = [];
    for (const e of rawErrors) {
      if (typeof e !== "object" || e === null) continue;
      const r = e as Record<string, unknown>;
      const entry: {
        message: string;
        path: Array<string | number>;
        validator?: string;
      } = {
        message: typeof r.message === "string" ? r.message : "",
        path: Array.isArray(r.path)
          ? (r.path as Array<string | number>)
          : [],
      };
      if (typeof r.validator === "string") entry.validator = r.validator;
      collected.push(entry);
    }
    this.errors = collected;

    const dlq = details ? (details as { deadletter_msg_id?: unknown }).deadletter_msg_id : undefined;
    this.deadletterMsgId = typeof dlq === "string" ? dlq : null;

    const channel = details ? (details as { channel?: unknown }).channel : undefined;
    this.channel = typeof channel === "string" ? channel : null;
  }
}

/**
 * Workflow step is invalid (e.g. name not in the manifest).
 *
 * Subclasses {@link InvalidArgumentsError} so existing handlers that catch
 * 400-class validation errors automatically pick this up. Raised both
 * client-side (when {@link WorkflowHandle.startStep} rejects an off-manifest
 * name before it leaves the box) and server-side (mapped from the
 * `INVALID_WORKFLOW_STEP` error code).
 */
export class InvalidWorkflowStepError extends InvalidArgumentsError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, status, details);
    // Override the code so consumers can switch on it.
    Object.defineProperty(this, "code", { value: "INVALID_WORKFLOW_STEP", enumerable: true });
    this.name = "InvalidWorkflowStepError";
  }
}

// ---------------------------------------------------------------------------
// 401 — auth
// ---------------------------------------------------------------------------

export class UnauthorizedError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "UNAUTHORIZED", status, details);
    this.name = "UnauthorizedError";
  }
}

/** v0.3 — token presented to identity service is malformed or unknown. */
export class InvalidTokenError extends UnauthorizedError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, status, details);
    Object.defineProperty(this, "code", { value: "INVALID_TOKEN", enumerable: true });
    this.name = "InvalidTokenError";
  }
}

/** v0.3 — token signature/format is fine but `exp` has elapsed. */
export class TokenExpiredError extends UnauthorizedError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, status, details);
    Object.defineProperty(this, "code", { value: "TOKEN_EXPIRED", enumerable: true });
    this.name = "TokenExpiredError";
  }
}

/** v0.3 — token was explicitly revoked. */
export class TokenRevokedError extends UnauthorizedError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, status, details);
    Object.defineProperty(this, "code", { value: "TOKEN_REVOKED", enumerable: true });
    this.name = "TokenRevokedError";
  }
}

// ---------------------------------------------------------------------------
// 404 — not-found family
// ---------------------------------------------------------------------------

export class WorkspaceNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "WORKSPACE_NOT_FOUND", status, details);
    this.name = "WorkspaceNotFoundError";
  }
}

export class KeyNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "KEY_NOT_FOUND", status, details);
    this.name = "KeyNotFoundError";
  }
}

export class FileNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "FILE_NOT_FOUND", status, details);
    this.name = "FileNotFoundError";
  }
}

export class SnapshotNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "SNAPSHOT_NOT_FOUND", status, details);
    this.name = "SnapshotNotFoundError";
  }
}

export class BranchNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "BRANCH_NOT_FOUND", status, details);
    this.name = "BranchNotFoundError";
  }
}

export class ToolNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "TOOL_NOT_FOUND", status, details);
    this.name = "ToolNotFoundError";
  }
}

/** v0.2 — channel does not exist on the workspace. */
export class ChannelNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "CHANNEL_NOT_FOUND", status, details);
    this.name = "ChannelNotFoundError";
  }
}

/** v0.2 — channel message is gone (already acked or never existed). */
export class MessageNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "MESSAGE_NOT_FOUND", status, details);
    this.name = "MessageNotFoundError";
  }
}

/** v0.2 — workflow does not exist on the workspace. */
export class WorkflowNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "WORKFLOW_NOT_FOUND", status, details);
    this.name = "WorkflowNotFoundError";
  }
}

/** v0.2 — workflow step ID does not exist. */
export class WorkflowStepNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "WORKFLOW_STEP_NOT_FOUND", status, details);
    this.name = "WorkflowStepNotFoundError";
  }
}

/** v0.4 — signing key does not exist on the identity service. */
export class SigningKeyNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "SIGNING_KEY_NOT_FOUND", status, details);
    this.name = "SigningKeyNotFoundError";
  }
}

// ---------------------------------------------------------------------------
// v0.6 — Generic resource locks
// ---------------------------------------------------------------------------

/**
 * The lock is currently held by a different holder (HTTP 409).
 *
 * Surfaces ``current_holder`` and ``retry_after_seconds`` from the
 * server's error envelope so callers don't have to reach into
 * {@link PlinthError.details}.
 */
export class LockConflictError extends PlinthError {
  /** Holder string of whoever owns the lock right now (when reported). */
  readonly currentHolder: string | null;
  /** Suggested back-off in seconds (when reported). */
  readonly retryAfterSeconds: number | null;

  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    // The workspace service emits ``LOCK_HELD`` today; the SDK reports it
    // as the spec-aligned ``LOCK_CONFLICT`` for forward compatibility with
    // future services that surface the spec code directly.
    super(message, "LOCK_CONFLICT", status, details);
    this.name = "LockConflictError";
    const ch = details?.current_holder;
    this.currentHolder = typeof ch === "string" ? ch : null;
    const ra = details?.retry_after_seconds;
    this.retryAfterSeconds = typeof ra === "number" ? ra : null;
  }
}

/**
 * Heartbeat / release attempted on a lock the caller does not hold.
 *
 * Either the row exists but a different holder owns it (the most common
 * case) or the caller's TTL elapsed and another holder stole the lock.
 */
export class LockNotHeldError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "LOCK_NOT_HELD", status, details);
    this.name = "LockNotHeldError";
  }
}

/** The requested lock does not exist (HTTP 404). */
export class LockNotFoundError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "LOCK_NOT_FOUND", status, details);
    this.name = "LockNotFoundError";
  }
}

// ---------------------------------------------------------------------------
// 429 — rate / cost
// ---------------------------------------------------------------------------

export class RateLimitedError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "RATE_LIMITED", status, details);
    this.name = "RateLimitedError";
  }
}

/** Specialised RateLimitedError for the cost-cap path. */
export class CostCapExceededError extends RateLimitedError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, status, details);
    Object.defineProperty(this, "code", { value: "COST_CAP_EXCEEDED", enumerable: true });
    this.name = "CostCapExceededError";
  }
}

// ---------------------------------------------------------------------------
// Tool failures
// ---------------------------------------------------------------------------

export class ToolInvocationError extends PlinthError {
  constructor(message: string, status?: number, details?: Record<string, unknown>) {
    super(message, "TOOL_INVOCATION_FAILED", status, details);
    this.name = "ToolInvocationError";
  }
}

// ---------------------------------------------------------------------------
// Envelope → typed error mapper
// ---------------------------------------------------------------------------

/**
 * Translate an HTTP response + parsed error envelope into the right typed
 * error subclass.
 *
 * Falls back to the base {@link PlinthError} when the code is not one we
 * recognise — callers can still rely on `code` and `status`.
 */
export function errorFromEnvelope(
  status: number,
  envelope: Partial<ErrorEnvelope> | null,
): PlinthError {
  const code = envelope?.error?.code ?? "INTERNAL_ERROR";
  const message = envelope?.error?.message ?? `Request failed with status ${status}`;
  const details = envelope?.error?.details as Record<string, unknown> | undefined;

  switch (code) {
    case "WORKSPACE_NOT_FOUND":
      return new WorkspaceNotFoundError(message, status, details);
    case "KEY_NOT_FOUND":
      return new KeyNotFoundError(message, status, details);
    case "FILE_NOT_FOUND":
      return new FileNotFoundError(message, status, details);
    case "SNAPSHOT_NOT_FOUND":
      return new SnapshotNotFoundError(message, status, details);
    case "BRANCH_NOT_FOUND":
      return new BranchNotFoundError(message, status, details);
    case "TOOL_NOT_FOUND":
      return new ToolNotFoundError(message, status, details);
    case "CHANNEL_NOT_FOUND":
      return new ChannelNotFoundError(message, status, details);
    case "MESSAGE_NOT_FOUND":
      return new MessageNotFoundError(message, status, details);
    case "WORKFLOW_NOT_FOUND":
      return new WorkflowNotFoundError(message, status, details);
    case "WORKFLOW_STEP_NOT_FOUND":
      return new WorkflowStepNotFoundError(message, status, details);
    case "SIGNING_KEY_NOT_FOUND":
      return new SigningKeyNotFoundError(message, status, details);
    case "INVALID_WORKFLOW_STEP":
      return new InvalidWorkflowStepError(message, status, details);
    case "TOOL_INVOCATION_FAILED":
      return new ToolInvocationError(message, status, details);
    case "SCHEMA_VIOLATION":
      return new SchemaViolationError(message, status, details);
    case "INVALID_ARGUMENTS":
      return new InvalidArgumentsError(message, status, details);
    case "UNAUTHORIZED":
      return new UnauthorizedError(message, status, details);
    case "INVALID_TOKEN":
      return new InvalidTokenError(message, status, details);
    case "TOKEN_EXPIRED":
      return new TokenExpiredError(message, status, details);
    case "TOKEN_REVOKED":
      return new TokenRevokedError(message, status, details);
    case "RATE_LIMITED":
      return new RateLimitedError(message, status, details);
    case "COST_CAP_EXCEEDED":
      return new CostCapExceededError(message, status, details);
    // v0.6 — generic resource locks. The workspace service emits the
    // ``LOCK_HELD`` code; we fold it into the SDK's spec-aligned
    // ``LockConflictError`` so user code reads naturally.
    case "LOCK_HELD":
    case "LOCK_CONFLICT":
      return new LockConflictError(message, status, details);
    case "LOCK_NOT_HELD":
      return new LockNotHeldError(message, status, details);
    case "LOCK_NOT_FOUND":
      return new LockNotFoundError(message, status, details);
    default:
      // Best-effort fallback: map by status if no matching code.
      if (status === 401) return new UnauthorizedError(message, status, details);
      if (status === 429) return new RateLimitedError(message, status, details);
      if (status === 400) return new InvalidArgumentsError(message, status, details);
      return new PlinthError(message, code, status, details);
  }
}
