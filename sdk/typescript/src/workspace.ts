/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Workspace, KV, Files, Snapshots, Branches, Channels, Workflows.
 *
 * The {@link Workspace} class is the entry point for all per-workspace
 * operations. It exposes typed sub-clients (`kv`, `files`, `channels`,
 * `workflows`) and raw methods for snapshots, branches, and diffs.
 *
 * `withBranch(branchId)` returns a thin wrapper that scopes every read
 * and write to the given branch — implemented by attaching `?branch=…`
 * to the underlying HTTP requests rather than duplicating client state.
 */

import { ChannelsClient, type ChannelsScope } from "./channels.js";
import { encodePath, type HttpClient, type QueryValue } from "./http.js";
import type {
  Branch,
  DiffResult,
  FileEntry,
  JsonValue,
  KVEntry,
  Lock,
  LockAcquireOptions,
  LockHeartbeatOptions,
  LockReleaseOptions,
  MergeResult,
  Snapshot,
  WithLockOptions,
  Workspace as WorkspaceModel,
} from "./types.js";
import { WorkflowsClient } from "./workflows.js";

/** Source of branch scoping passed to per-resource sub-clients. */
interface BranchScope {
  readonly branchId: string | null;
}

/**
 * Versioned key-value store for a single workspace.
 *
 * Reads return the latest version by default. Pass `version` on
 * {@link KVClient.getWithMeta} to fetch a specific historical revision.
 */
export class KVClient {
  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
    private readonly scope: BranchScope,
  ) {}

  /** Write a value. Always creates a new version. */
  async set(key: string, value: JsonValue): Promise<KVEntry> {
    return this.http.requestJson<KVEntry>({
      method: "PUT",
      path: this.keyPath(key),
      query: this.scopedQuery(),
      json: { value },
    });
  }

  /** Read the latest value, or `null` if the key was deleted/never set. */
  async get(key: string): Promise<JsonValue | null> {
    const entry = await this.fetchEntry(key);
    return entry.deleted ? null : entry.value;
  }

  /** Read the latest entry along with its version metadata. */
  async getWithMeta(key: string, opts: { version?: number } = {}): Promise<KVEntry> {
    return this.fetchEntry(key, opts.version);
  }

  /** All historical versions of a key, oldest → newest. */
  async history(key: string): Promise<KVEntry[]> {
    const res = await this.http.requestJson<{ versions: KVEntry[] }>({
      method: "GET",
      path: `${this.keyPath(key)}/history`,
      query: this.scopedQuery(),
    });
    return res.versions;
  }

  /** Tombstone the key. Reads after this return `null`. */
  async delete(key: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: this.keyPath(key),
      query: this.scopedQuery(),
    });
  }

  /** Latest entry for every key in the workspace. */
  async list(): Promise<KVEntry[]> {
    const res = await this.http.requestJson<{ entries: KVEntry[] }>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/kv`,
      query: this.scopedQuery(),
    });
    return res.entries;
  }

  private fetchEntry(key: string, version?: number): Promise<KVEntry> {
    const query = this.scopedQuery();
    if (version !== undefined) query.version = version;
    return this.http.requestJson<KVEntry>({
      method: "GET",
      path: this.keyPath(key),
      query,
    });
  }

  private keyPath(key: string): string {
    return `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/kv/${encodeURIComponent(key)}`;
  }

  private scopedQuery(): Record<string, QueryValue> {
    const q: Record<string, QueryValue> = {};
    if (this.scope.branchId) q.branch = this.scope.branchId;
    return q;
  }
}

/**
 * Versioned file/blob store for a single workspace.
 *
 * Files are addressed by path (`reports/q1.md`). Use {@link FilesClient.read}
 * for raw bytes or {@link FilesClient.readText} for UTF-8 text.
 */
export class FilesClient {
  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
    private readonly scope: BranchScope,
  ) {}

  /** Write a file. `content` may be a string or any binary buffer. */
  async write(
    path: string,
    content: string | Uint8Array | ArrayBuffer,
    opts: { contentType?: string } = {},
  ): Promise<FileEntry> {
    return this.http.requestJson<FileEntry>({
      method: "PUT",
      path: this.filePath(path),
      query: this.scopedQuery(),
      bytes: content,
      contentType:
        opts.contentType ??
        (typeof content === "string" ? "text/plain; charset=utf-8" : undefined),
    });
  }

  /** Read raw bytes. */
  async read(path: string, opts: { version?: number } = {}): Promise<Uint8Array> {
    const query = this.scopedQuery();
    if (opts.version !== undefined) query.version = opts.version;
    return this.http.requestBytes({
      method: "GET",
      path: this.filePath(path),
      query,
    });
  }

  /** Read bytes and decode as UTF-8 text. */
  async readText(path: string, opts: { version?: number } = {}): Promise<string> {
    const bytes = await this.read(path, opts);
    return new TextDecoder("utf-8").decode(bytes);
  }

  /** Fetch metadata only — size, sha256, version, etc. */
  async meta(path: string): Promise<FileEntry> {
    return this.http.requestJson<FileEntry>({
      method: "GET",
      path: `${this.filePath(path)}/meta`,
      query: this.scopedQuery(),
    });
  }

  /** Tombstone the file. */
  async delete(path: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: this.filePath(path),
      query: this.scopedQuery(),
    });
  }

  /** Latest metadata for every file in the workspace. */
  async list(): Promise<FileEntry[]> {
    const res = await this.http.requestJson<{ files: FileEntry[] }>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/files`,
      query: this.scopedQuery(),
    });
    return res.files;
  }

  private filePath(path: string): string {
    // File paths can contain slashes — encode each segment but keep them.
    const stripped = path.replace(/^\/+/, "");
    return `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/files/${encodePath(stripped)}`;
  }

  private scopedQuery(): Record<string, QueryValue> {
    const q: Record<string, QueryValue> = {};
    if (this.scope.branchId) q.branch = this.scope.branchId;
    return q;
  }
}

/**
 * SnapshotProxy — explicit handle for snapshot operations.
 *
 * Mirrors the Python SDK's `ws._snapshots` object. The {@link Workspace}
 * facade exposes ergonomic shortcuts (`ws.snapshot(...)`,
 * `ws.listSnapshots()`, `ws.diff(...)`) that delegate here.
 */
export class SnapshotProxy {
  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
    private readonly scope: BranchScope,
  ) {}

  async create(name: string, opts: { message?: string } = {}): Promise<Snapshot> {
    const body: Record<string, JsonValue> = { name };
    if (opts.message !== undefined) body.message = opts.message;
    return this.http.requestJson<Snapshot>({
      method: "POST",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/snapshots`,
      query: this.scopedQuery(),
      json: body,
    });
  }

  async list(): Promise<Snapshot[]> {
    const res = await this.http.requestJson<{ snapshots: Snapshot[] }>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/snapshots`,
      query: this.scopedQuery(),
    });
    return res.snapshots;
  }

  async get(snapshotId: string): Promise<Snapshot> {
    return this.http.requestJson<Snapshot>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/snapshots/${encodeURIComponent(snapshotId)}`,
    });
  }

  async diff(snapshotA: string, snapshotB: string): Promise<DiffResult> {
    return this.http.requestJson<DiffResult>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/snapshots/${encodeURIComponent(snapshotA)}/diff`,
      query: { against: snapshotB },
    });
  }

  private scopedQuery(): Record<string, QueryValue> {
    const q: Record<string, QueryValue> = {};
    if (this.scope.branchId) q.branch = this.scope.branchId;
    return q;
  }
}

/**
 * v0.6 — generic distributed locks over named workspace resources.
 *
 * Locks are independent of the workflow-step lease primitive. Use them
 * to coordinate any named object (KV key, file path, external resource
 * handle) across multiple agents.
 *
 * @example
 * ```ts
 * await ws.locks.withLock(
 *   "kv:sources/index",
 *   "agent-A",
 *   { ttlSeconds: 30 },
 *   async () => {
 *     await ws.kv.set("sources/index", newValue);
 *   },
 * );
 * ```
 */
export class LocksClient {
  constructor(
    private readonly http: HttpClient,
    private readonly workspaceId: string,
  ) {}

  /**
   * Acquire a lock on `name`.
   *
   * On contention either rejects with `LockConflictError` immediately
   * (default) or polls the server until `waitMs` elapses.
   */
  async acquire(name: string, opts: LockAcquireOptions): Promise<Lock> {
    return this.http.requestJson<Lock>({
      method: "POST",
      path: `${this.namedPath(name)}/acquire`,
      json: {
        holder: opts.holder,
        ttl_seconds: opts.ttlSeconds ?? 60,
        wait_ms: opts.waitMs ?? 0,
      },
    });
  }

  /** Extend the lock's TTL. Only the current holder may heartbeat. */
  async heartbeat(name: string, opts: LockHeartbeatOptions): Promise<Lock> {
    const body: Record<string, JsonValue> = { holder: opts.holder };
    if (opts.ttlSeconds !== undefined) body.ttl_seconds = opts.ttlSeconds;
    return this.http.requestJson<Lock>({
      method: "POST",
      path: `${this.namedPath(name)}/heartbeat`,
      json: body,
    });
  }

  /** Release a held lock. Idempotent — no error if it's already gone. */
  async release(name: string, opts: LockReleaseOptions): Promise<void> {
    await this.http.requestVoid({
      method: "POST",
      path: `${this.namedPath(name)}/release`,
      json: { holder: opts.holder },
    });
  }

  /** Return every lock currently persisted in this workspace. */
  async list(): Promise<Lock[]> {
    const res = await this.http.requestJson<{ locks: Lock[] }>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/locks`,
    });
    return res.locks;
  }

  /** Fetch a single lock row. Throws `LockNotFoundError` on miss. */
  async get(name: string): Promise<Lock> {
    return this.http.requestJson<Lock>({
      method: "GET",
      path: this.namedPath(name),
    });
  }

  /**
   * Acquire `name`, run `fn`, then release — guaranteed even on rejection.
   *
   * While `fn` runs, an interval-driven heartbeat keeps the lock alive
   * (default cadence `30 seconds`; pass `heartbeatIntervalMs: 0` to
   * disable). The heartbeat stops cleanly when `fn` resolves *or*
   * rejects, and the lock is released either way.
   */
  async withLock<T>(
    name: string,
    holder: string,
    opts: WithLockOptions,
    fn: () => Promise<T>,
  ): Promise<T> {
    const ttlSeconds = opts.ttlSeconds ?? 60;
    const waitMs = opts.waitMs ?? 0;
    const heartbeatIntervalMs = opts.heartbeatIntervalMs ?? 30_000;

    await this.acquire(name, { holder, ttlSeconds, waitMs });

    let timer: ReturnType<typeof setInterval> | null = null;
    if (heartbeatIntervalMs > 0) {
      timer = setInterval(() => {
        // Fire-and-forget: a transient heartbeat failure shouldn't
        // crash the body, and a permanent one will surface on the
        // next ``acquire`` by another holder.
        this.heartbeat(name, { holder }).catch(() => {});
      }, heartbeatIntervalMs);
      // Don't keep the Node process alive just for a lock heartbeat
      // (browsers don't expose ``unref`` so we feature-detect).
      const handle = timer as { unref?: () => void };
      if (typeof handle.unref === "function") handle.unref();
    }

    try {
      return await fn();
    } finally {
      if (timer !== null) clearInterval(timer);
      // Release is idempotent — never let a release failure mask the
      // body's outcome.
      try {
        await this.release(name, { holder });
      } catch {
        // ignore
      }
    }
  }

  private namedPath(name: string): string {
    // ``name`` may contain ``/`` and ``:`` (the canonical case for
    // prefixed names like ``kv:sources/index``) — preserve them so the
    // workspace's ``{name:path}`` route consumes the whole thing as
    // one segment. ``:`` is a sub-delim per RFC 3986 and is safe inside
    // a path component.
    const stripped = name.replace(/^\/+/, "");
    const encoded = stripped
      .split("/")
      .map((part) => encodeURIComponent(part).replace(/%3A/gi, ":"))
      .join("/");
    return `/v1/workspaces/${encodeURIComponent(this.workspaceId)}/locks/${encoded}`;
  }
}


/**
 * A handle to a workspace, plus all of its sub-clients.
 *
 * Construct via {@link Plinth.workspace} — never directly. Two
 * {@link Workspace} instances may share an underlying record on the
 * server but differ in their branch scope (see {@link Workspace.withBranch}).
 */
export class Workspace {
  /** Versioned key-value store for this workspace. */
  readonly kv: KVClient;
  /** Versioned file/blob store for this workspace. */
  readonly files: FilesClient;
  /** v0.2 — typed, persistent message queues. */
  readonly channels: ChannelsClient;
  /** v0.2 — durable, resumable agent pipelines. */
  readonly workflows: WorkflowsClient;
  /** v0.6 — generic distributed locks over named workspace resources. */
  readonly locks: LocksClient;

  /** Stable workspace ID (`ws_<ulid>`). */
  readonly id: string;
  /** Human-readable name supplied at creation time. */
  readonly name: string;
  /** Underlying server record at the moment this client was constructed. */
  readonly record: WorkspaceModel;
  /** Branch this client is scoped to, or `null` for main. */
  readonly branchId: string | null;

  private readonly snapshots: SnapshotProxy;

  constructor(
    private readonly http: HttpClient,
    record: WorkspaceModel,
    branchId: string | null = null,
  ) {
    this.record = record;
    this.id = record.id;
    this.name = record.name;
    this.branchId = branchId;
    const scope: BranchScope & ChannelsScope = { branchId };
    this.kv = new KVClient(http, record.id, scope);
    this.files = new FilesClient(http, record.id, scope);
    this.snapshots = new SnapshotProxy(http, record.id, scope);
    this.channels = new ChannelsClient(http, record.id, scope);
    // Workflows aren't branch-scoped server-side, but we still pass the
    // shared HTTP client so they participate in the same auth context.
    this.workflows = new WorkflowsClient(http, record.id);
    // Locks aren't branch-scoped — they're a workspace-level coordination
    // primitive — so they share the same HTTP client without scope.
    this.locks = new LocksClient(http, record.id);
  }

  /**
   * Return a new {@link Workspace} that scopes all reads and writes to
   * the given branch. The original instance is unchanged.
   */
  withBranch(branchId: string): Workspace {
    return new Workspace(this.http, this.record, branchId);
  }

  /** Create a snapshot capturing the current latest version of every key/file. */
  async snapshot(name: string, opts: { message?: string } = {}): Promise<Snapshot> {
    return this.snapshots.create(name, opts);
  }

  /** List all snapshots for this workspace. */
  async listSnapshots(): Promise<Snapshot[]> {
    return this.snapshots.list();
  }

  /** Fetch a single snapshot by ID. */
  async getSnapshot(snapshotId: string): Promise<Snapshot> {
    return this.snapshots.get(snapshotId);
  }

  /** Diff snapshot `a` against snapshot `b`. */
  async diff(snapshotA: string, snapshotB: string): Promise<DiffResult> {
    return this.snapshots.diff(snapshotA, snapshotB);
  }

  /** Create a branch from a snapshot. */
  async branch(name: string, opts: { fromSnapshot: string }): Promise<Branch> {
    return this.http.requestJson<Branch>({
      method: "POST",
      path: `/v1/workspaces/${encodeURIComponent(this.id)}/branches`,
      json: { name, from_snapshot: opts.fromSnapshot },
    });
  }

  /** List all branches for this workspace. */
  async listBranches(): Promise<Branch[]> {
    const res = await this.http.requestJson<{ branches: Branch[] }>({
      method: "GET",
      path: `/v1/workspaces/${encodeURIComponent(this.id)}/branches`,
    });
    return res.branches;
  }

  /** Merge a branch back into main. */
  async merge(branchId: string): Promise<MergeResult> {
    return this.http.requestJson<MergeResult>({
      method: "POST",
      path: `/v1/workspaces/${encodeURIComponent(this.id)}/branches/${encodeURIComponent(branchId)}/merge`,
    });
  }

  /** Delete a branch (does not affect main). */
  async deleteBranch(branchId: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: `/v1/workspaces/${encodeURIComponent(this.id)}/branches/${encodeURIComponent(branchId)}`,
    });
  }
}
