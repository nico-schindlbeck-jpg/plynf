/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Workspace channels — typed, persistent message queues.
 *
 * A channel is a workspace-scoped FIFO of {@link ChannelMessage} objects with
 * a monotonic per-channel `seq`. Channels are created lazily on the first
 * `send`. Receives can either start from the beginning (`since=0`) or
 * resume a named consumer's server-tracked cursor.
 *
 * Mirrors `plinth.channels.ChannelsProxy` in the Python SDK.
 */

import { ChannelNotFoundError } from "./errors.js";
import { encodePath, type HttpClient, type QueryValue } from "./http.js";
import type {
  Channel,
  ChannelMessage,
  ChannelSchema,
  ChannelSendBody,
  JsonValue,
  ReplayBatchResult,
  SchemaCheckResult,
} from "./types.js";

/** Encode a channel name (or message ID) for safe URL embedding. */
function encodeName(name: string): string {
  return encodeURIComponent(name);
}

/** Options accepted by {@link ChannelsClient.send}. */
export interface ChannelSendOptions {
  /** Optional descriptive label (e.g. agent ID). */
  sender?: string;
  /** Optional message type for filtering on the receive side. */
  type?: string;
  /** Optional correlation key for request/response patterns. */
  correlationId?: string;
  /** Optional string-string metadata. */
  headers?: Record<string, string>;
}

/** Options accepted by {@link ChannelsClient.receive}. */
export interface ChannelReceiveOptions {
  /**
   * Named consumer. The server tracks a per-consumer cursor so
   * subsequent calls without {@link ChannelReceiveOptions.since} resume
   * where the last one left off.
   */
  consumer?: string;
  /** Explicit sequence override — returns messages with `seq > since`. */
  since?: number;
  /** Maximum messages (server default 100, max 1000). */
  limit?: number;
  /** When `true`, the consumer cursor is not advanced. */
  peek?: boolean;
}

/** Options accepted by {@link ChannelsClient.wait}. */
export interface ChannelWaitOptions {
  /** Named consumer (server-tracked cursor). */
  consumer?: string;
  /** Total wall-clock milliseconds to wait. Default `30_000`. */
  timeoutMs?: number;
  /** Milliseconds between polls. Default `500`. */
  pollIntervalMs?: number;
}

/** Source of branch scoping (kept by the parent {@link Workspace}). */
export interface ChannelsScope {
  readonly branchId: string | null;
}

/**
 * Client for the v0.2 Channels API on a workspace.
 *
 * Reachable via `ws.channels`. Direct construction is supported but
 * discouraged — the workspace builds and caches one of these on first
 * access.
 */
export class ChannelsClient {
  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
    private readonly scope: ChannelsScope,
  ) {}

  /**
   * Send a payload on `channel`. Creates the channel on first use.
   *
   * @returns the persisted {@link ChannelMessage} with server-assigned
   *          `id`, `seq`, and `sent_at`.
   */
  async send(
    channel: string,
    payload: JsonValue,
    opts: ChannelSendOptions = {},
  ): Promise<ChannelMessage> {
    const body: ChannelSendBody = { payload };
    if (opts.sender !== undefined) body.sender = opts.sender;
    if (opts.type !== undefined) body.type = opts.type;
    if (opts.correlationId !== undefined) body.correlation_id = opts.correlationId;
    if (opts.headers !== undefined) body.headers = opts.headers;

    return this.http.requestJson<ChannelMessage>({
      method: "POST",
      path: this.path(`/channels/${encodeName(channel)}/send`),
      query: this.scopedQuery(),
      json: body as unknown as JsonValue,
    });
  }

  /** Receive a batch of messages from `channel`. */
  async receive(
    channel: string,
    opts: ChannelReceiveOptions = {},
  ): Promise<ChannelMessage[]> {
    const query: Record<string, QueryValue> = { ...this.scopedQuery() };
    if (opts.consumer !== undefined) query.consumer = opts.consumer;
    if (opts.since !== undefined) query.since = opts.since;
    if (opts.limit !== undefined) query.limit = opts.limit;
    if (opts.peek) query.peek = "true";

    const res = await this.http.requestJson<{ messages: ChannelMessage[] }>({
      method: "GET",
      path: this.path(`/channels/${encodeName(channel)}/receive`),
      query,
    });
    return res.messages ?? [];
  }

  /**
   * Acknowledge (delete) a message on the server.
   *
   * Accepts a full {@link ChannelMessage} so the channel name can be read
   * off the model — passing only an ID would not be enough to build the
   * DELETE URL.
   */
  async ack(message: ChannelMessage): Promise<void> {
    if (typeof message !== "object" || message === null || !("channel" in message)) {
      throw new TypeError(
        "ChannelsClient.ack requires a ChannelMessage object — pass the message you received, not just an ID.",
      );
    }
    await this.http.requestVoid({
      method: "DELETE",
      path: this.path(
        `/channels/${encodeName(message.channel)}/messages/${encodeName(message.id)}`,
      ),
    });
  }

  /** Alias for {@link ack} — mirrors the spec's "delete" verb. */
  async delete(message: ChannelMessage): Promise<void> {
    return this.ack(message);
  }

  /**
   * Block-wait for a single message via polling.
   *
   * Returns `null` on timeout. Uses {@link ChannelsClient.receive} with
   * `limit=1` under the hood.
   */
  async wait(channel: string, opts: ChannelWaitOptions = {}): Promise<ChannelMessage | null> {
    const timeoutMs = opts.timeoutMs ?? 30_000;
    const pollIntervalMs = opts.pollIntervalMs ?? 500;
    const deadline = Date.now() + Math.max(0, timeoutMs);

    while (true) {
      const msgs = await this.receive(channel, {
        ...(opts.consumer !== undefined ? { consumer: opts.consumer } : {}),
        limit: 1,
      });
      if (msgs.length > 0) return msgs[0]!;
      const remaining = deadline - Date.now();
      if (remaining <= 0) return null;
      await sleep(Math.min(pollIntervalMs, remaining));
    }
  }

  /** List every channel on the workspace. */
  async list(): Promise<Channel[]> {
    const res = await this.http.requestJson<{ channels: Channel[] }>({
      method: "GET",
      path: this.path("/channels"),
      query: this.scopedQuery(),
    });
    return res.channels ?? [];
  }

  /** Fetch a single channel by name. */
  async get(channel: string): Promise<Channel> {
    return this.http.requestJson<Channel>({
      method: "GET",
      path: this.path(`/channels/${encodeName(channel)}`),
      query: this.scopedQuery(),
    });
  }

  /** Delete a channel and all of its messages. */
  async deleteChannel(channel: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: this.path(`/channels/${encodeName(channel)}`),
      query: this.scopedQuery(),
    });
  }

  // ------------------------------------------------------------------
  // v0.5 — typed channels: schema CRUD
  // ------------------------------------------------------------------

  /**
   * Attach a JSON Schema to {@link channel}.
   *
   * Each call increments the channel's schema version. Subsequent sends
   * are validated; failures land on the channel's dead-letter queue and
   * raise {@link SchemaViolationError}.
   */
  async setSchema(
    channel: string,
    schema: Record<string, JsonValue>,
  ): Promise<ChannelSchema> {
    return this.http.requestJson<ChannelSchema>({
      method: "POST",
      path: this.path(`/channels/${encodeName(channel)}/schema`),
      json: { schema } as unknown as JsonValue,
    });
  }

  /**
   * Return the schema attached to {@link channel}, or `null` if unset.
   *
   * Both shapes of 404 ("no channel" and "no schema on this channel") are
   * folded down to `null` because callers usually want to know whether a
   * schema is in effect, not why it isn't.
   */
  async getSchema(channel: string): Promise<ChannelSchema | null> {
    try {
      return await this.http.requestJson<ChannelSchema>({
        method: "GET",
        path: this.path(`/channels/${encodeName(channel)}/schema`),
      });
    } catch (err) {
      if (err instanceof ChannelNotFoundError) return null;
      // The server emits a SCHEMA_NOT_FOUND code for missing schemas; the
      // generic envelope mapper folds that into the base PlinthError. Use
      // a code check to avoid re-classifying genuine errors.
      const e = err as { code?: string; status?: number };
      if (e?.code === "SCHEMA_NOT_FOUND" || e?.status === 404) return null;
      throw err;
    }
  }

  /** Detach the schema from {@link channel}. Idempotent. */
  async deleteSchema(channel: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: this.path(`/channels/${encodeName(channel)}/schema`),
    });
  }

  // ------------------------------------------------------------------
  // v0.5 — dead-letter queue
  // ------------------------------------------------------------------

  /**
   * List dead-lettered messages for {@link channel}.
   *
   * Messages land here when their payload fails JSON Schema validation at
   * send time. They carry diagnostic `x-` headers (`x-original-channel`,
   * `x-validation-errors`, `x-failed-at`, `x-schema-version`) so consumers
   * can decide whether to replay, rewrite, or drop.
   */
  async deadletter(
    channel: string,
    opts: { limit?: number; since?: number } = {},
  ): Promise<ChannelMessage[]> {
    const query: Record<string, QueryValue> = {};
    if (opts.limit !== undefined) query.limit = opts.limit;
    if (opts.since !== undefined) query.since = opts.since;
    const res = await this.http.requestJson<{ messages: ChannelMessage[] }>({
      method: "GET",
      path: this.path(`/channels/${encodeName(channel)}/deadletter`),
      query,
    });
    return res.messages ?? [];
  }

  /**
   * Re-validate + re-send a DLQ message to its main channel.
   *
   * On success the original DLQ row is removed and the freshly-sent
   * message (with a new `id` and `seq`) is returned. If validation still
   * fails, {@link SchemaViolationError} is thrown and the DLQ row stays
   * put.
   */
  async replay(
    channel: string,
    msg: ChannelMessage | string,
  ): Promise<ChannelMessage> {
    const id = typeof msg === "string" ? msg : msg.id;
    return this.http.requestJson<ChannelMessage>({
      method: "POST",
      path: this.path(
        `/channels/${encodeName(channel)}/deadletter/${encodeName(id)}/replay`,
      ),
    });
  }

  /** Delete a DLQ message without replay (give-up path). */
  async dropDeadletter(
    channel: string,
    msg: ChannelMessage | string,
  ): Promise<void> {
    const id = typeof msg === "string" ? msg : msg.id;
    await this.http.requestVoid({
      method: "DELETE",
      path: this.path(
        `/channels/${encodeName(channel)}/deadletter/${encodeName(id)}`,
      ),
    });
  }

  // ------------------------------------------------------------------
  // v0.6 — channel schema migration helpers
  // ------------------------------------------------------------------

  /**
   * Preview compatibility of a candidate JSON Schema against existing rows.
   *
   * Validates up to `limit` messages (server hard cap: 10 000) drawn from
   * the main channel, the DLQ, or both. The candidate is not persisted —
   * pair this with `setSchema` once you're happy with the report. Returns
   * counts plus up to 10 failure samples in the canonical `{msg_id, errors}`
   * shape.
   */
  async checkSchema(
    channel: string,
    schema: object,
    opts: { scope?: "main" | "deadletter" | "both"; limit?: number } = {},
  ): Promise<SchemaCheckResult> {
    const body: Record<string, JsonValue> = {
      schema: schema as JsonValue,
      scope: opts.scope ?? "both",
      limit: opts.limit ?? 1000,
    };
    return this.http.requestJson<SchemaCheckResult>({
      method: "POST",
      path: this.path(`/channels/${encodeName(channel)}/schema/check`),
      json: body as JsonValue,
    });
  }

  /**
   * Bulk-replay DLQ messages back through the current schema.
   *
   * Iterates the DLQ in seq order (up to `max`, server hard cap 10 000).
   * Each message is re-validated against the *currently attached* schema;
   * successes move to the main channel, failures stay in the DLQ. With
   * `dryRun=true` no rows are mutated — the result still reflects what
   * would happen.
   *
   * `failures` is bounded server-side to 50 entries; the totals
   * (`attempted` / `succeeded` / `failed`) are accurate regardless of
   * truncation.
   */
  async replayAllDlq(
    channel: string,
    opts: { max?: number; dryRun?: boolean } = {},
  ): Promise<ReplayBatchResult> {
    const query: Record<string, QueryValue> = { max: opts.max ?? 1000 };
    if (opts.dryRun) query.dry_run = "true";
    return this.http.requestJson<ReplayBatchResult>({
      method: "POST",
      path: this.path(
        `/channels/${encodeName(channel)}/deadletter/replay-all`,
      ),
      query,
    });
  }

  /**
   * Delete DLQ rows older than `olderThanSeconds` and return the count.
   *
   * `olderThanSeconds=0` clears the entire DLQ — useful after a big
   * schema relax. Channels that never had a DLQ return `0` rather than
   * raising (same idempotency principle as `deleteSchema`).
   */
  async purgeDlq(
    channel: string,
    opts: { olderThanSeconds?: number } = {},
  ): Promise<number> {
    const query: Record<string, QueryValue> = {
      older_than_seconds: opts.olderThanSeconds ?? 0,
    };
    const res = await this.http.requestJson<{ purged: number }>({
      method: "DELETE",
      path: this.path(`/channels/${encodeName(channel)}/deadletter`),
      query,
    });
    return res.purged ?? 0;
  }

  // -- helpers ---------------------------------------------------------

  private path(suffix: string): string {
    return `/v1/workspaces/${encodePath(this.workspaceId)}${suffix}`;
  }

  private scopedQuery(): Record<string, QueryValue> {
    const q: Record<string, QueryValue> = {};
    if (this.scope.branchId) q.branch = this.scope.branchId;
    return q;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
