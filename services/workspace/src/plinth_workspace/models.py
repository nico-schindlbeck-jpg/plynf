# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Pydantic models for the workspace service.

These models intentionally mirror ``CONTRACTS.md → Pydantic Models``. Keep
them in lockstep — if you have to change one, change the spec first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Domain resources


class Workspace(BaseModel):
    """Top-level isolation boundary for an agent's state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    tenant_id: str = "default"
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class KVEntry(BaseModel):
    """A single immutable version of a KV write."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    key: str
    value: Any
    version: int
    created_at: datetime
    deleted: bool = False
    branch_id: str | None = None


class FileEntry(BaseModel):
    """Metadata for a single immutable version of a file."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    path: str
    size: int
    sha256: str
    content_type: str
    version: int
    created_at: datetime
    deleted: bool = False
    branch_id: str | None = None


class Snapshot(BaseModel):
    """An immutable point-in-time capture over a workspace."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    name: str
    message: str | None = None
    created_at: datetime
    kv_versions: dict[str, int] = Field(default_factory=dict)
    file_versions: dict[str, int] = Field(default_factory=dict)
    parent_snapshot_id: str | None = None


class Branch(BaseModel):
    """A divergent timeline rooted at a snapshot."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    name: str
    from_snapshot_id: str
    created_at: datetime
    merged: bool = False
    merged_at: datetime | None = None


class DiffResult(BaseModel):
    """Difference between two snapshots."""

    model_config = ConfigDict(extra="forbid")

    kv_added: list[str] = Field(default_factory=list)
    kv_modified: list[str] = Field(default_factory=list)
    kv_deleted: list[str] = Field(default_factory=list)
    files_added: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Request bodies (not in CONTRACTS, but needed for typed FastAPI handlers)


class WorkspaceCreate(BaseModel):
    """Body for ``POST /v1/workspaces``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KVWrite(BaseModel):
    """Body for ``PUT /v1/workspaces/{ws}/kv/{key}``."""

    model_config = ConfigDict(extra="forbid")

    value: Any


class SnapshotCreate(BaseModel):
    """Body for ``POST /v1/workspaces/{ws}/snapshots``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    message: str | None = None


class BranchCreate(BaseModel):
    """Body for ``POST /v1/workspaces/{ws}/branches``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    from_snapshot: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Collection responses (not in CONTRACTS, but mirror the JSON shape there)


class WorkspaceList(BaseModel):
    workspaces: list[Workspace]


class Tenant(BaseModel):
    """One tenant visible to the caller."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_count: int = 0


class TenantList(BaseModel):
    tenants: list[Tenant]


class KVList(BaseModel):
    entries: list[KVEntry]


class KVHistory(BaseModel):
    versions: list[KVEntry]


class FileList(BaseModel):
    files: list[FileEntry]


class SnapshotList(BaseModel):
    snapshots: list[Snapshot]


class BranchList(BaseModel):
    branches: list[Branch]


class MergeResult(BaseModel):
    """Result of merging a branch back into main."""

    model_config = ConfigDict(extra="forbid")

    branch_id: str
    workspace_id: str
    kv_merged: list[str] = Field(default_factory=list)
    files_merged: list[str] = Field(default_factory=list)
    merged_at: datetime


# ---------------------------------------------------------------------------
# v0.2 — Channels


class ChannelSendBody(BaseModel):
    """Body for ``POST /v1/workspaces/{ws}/channels/{name}/send``."""

    model_config = ConfigDict(extra="forbid")

    payload: Any
    sender: str | None = None
    type: str | None = None
    correlation_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class ChannelMessage(BaseModel):
    """A single message persisted on a channel."""

    model_config = ConfigDict(extra="forbid")

    id: str
    channel: str
    workspace_id: str
    seq: int
    payload: Any
    sender: str | None = None
    type: str | None = None
    correlation_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    sent_at: datetime
    delivered_at: datetime | None = None


class Channel(BaseModel):
    """A typed message queue scoped to a workspace."""

    model_config = ConfigDict(extra="forbid")

    name: str
    workspace_id: str
    message_count: int
    created_at: datetime
    last_send_at: datetime | None = None
    last_receive_at: datetime | None = None


class ChannelList(BaseModel):
    channels: list[Channel]


class ChannelMessages(BaseModel):
    messages: list[ChannelMessage]


# ---------------------------------------------------------------------------
# v0.5 — Typed channels: schemas + dead-letter queue


class ChannelSchema(BaseModel):
    """A JSON Schema attached to a channel.

    Channels with a schema validate every payload at send time. Failed
    validations go to a hidden ``<channel>.deadletter`` sub-channel and the
    caller gets a 422 ``SCHEMA_VIOLATION``. The ``version`` field
    auto-increments on each ``set_schema`` so clients can detect when a
    DLQ message was queued under a different schema.
    """

    # Pydantic v2 reserves ``schema_`` as a protected namespace so it can
    # warn loudly about field names that collide with the deprecated
    # ``BaseModel.schema_json()`` method. Our wire field is literally called
    # ``schema_json`` per CONTRACTS.md, so we opt out of the namespace
    # check here.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    workspace_id: str
    channel_name: str
    schema_json: dict[str, Any]
    version: int = 1
    updated_at: datetime


class ChannelSchemaSetBody(BaseModel):
    """Body for ``POST .../channels/{name}/schema``.

    The wire field is named ``schema`` to match :doc:`/CONTRACTS.md`. Pydantic
    treats ``schema`` as a reserved attribute on the model itself, so we
    alias to ``schema_doc`` internally and expose the original name on the
    JSON payload via ``Field(alias=...)``.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        protected_namespaces=(),
    )

    schema_doc: dict[str, Any] = Field(alias="schema")


# ---------------------------------------------------------------------------
# v0.6 — Channel schema migration helpers
# ---------------------------------------------------------------------------


SchemaCheckScope = Literal["main", "deadletter", "both"]


class SchemaCheckBody(BaseModel):
    """Body for ``POST .../channels/{name}/schema/check`` (v0.6).

    Submits a candidate JSON Schema document for compatibility validation
    without persisting it. The wire field is named ``schema`` (alias) to
    match the operator-facing surface; we map to ``schema_doc`` internally
    for the same reason as :class:`ChannelSchemaSetBody`.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        protected_namespaces=(),
    )

    schema_doc: dict[str, Any] = Field(alias="schema")
    scope: SchemaCheckScope = "both"
    # Bound the iteration so ``check`` can never wedge on a million-row DLQ.
    # The hard cap of 10 000 mirrors the spec; the default of 1000 is a
    # reasonable preview window for migration planning.
    limit: int = Field(default=1000, ge=1, le=10000)


class SchemaCheckResult(BaseModel):
    """Outcome of a ``schema/check`` invocation.

    ``sample_failures`` is bounded to 10 entries so a runaway ``invalid``
    count never produces a multi-megabyte response. Each entry has the
    consistent shape ``{"msg_id": str, "errors": [{path, message, ...}]}``
    so SDK callers can render diagnostics without case-by-case parsing.
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    scope: SchemaCheckScope
    checked: int = 0
    valid: int = 0
    invalid: int = 0
    sample_failures: list[dict[str, Any]] = Field(default_factory=list)


class ReplayBatchBody(BaseModel):
    """Optional JSON body for ``POST .../deadletter/replay-all`` (v0.6).

    The endpoint accepts either query parameters (``?dry_run=&max=``) or
    this JSON body — the body form is the documented "recommended" shape
    in :doc:`/CONTRACTS.md` because replay is a mutating operation. Both
    fields are optional so an empty ``{}`` is a valid request.
    """

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
    # Bounded to ``BULK_HARD_LIMIT`` on the store (10_000); the default
    # 100 mirrors the spec's "cap per call" recommendation.
    max: int = Field(default=100, ge=1, le=10000)  # noqa: A003


class ReplayBatchResult(BaseModel):
    """Outcome of a ``deadletter/replay-all`` invocation.

    ``failures`` is bounded to 50 entries (each ``{"msg_id": str, "reason":
    str}``) — enough to surface common patterns without unbounded growth.
    ``dry_run`` is echoed so callers can distinguish "would succeed" from
    "did succeed" results without re-checking their own request.
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = False


class PurgeDLQResult(BaseModel):
    """Outcome of ``DELETE .../deadletter`` (purge).

    Aliased as ``DLQPurgeResult`` in the SDK. The single ``purged`` int is
    the row count actually removed; channels without a DLQ return 0.
    """

    model_config = ConfigDict(extra="forbid")

    purged: int = 0


# ---------------------------------------------------------------------------
# v0.2 — Workflows


WorkflowStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
WorkflowStepStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class WorkflowCreate(BaseModel):
    """Body for ``POST /v1/workspaces/{ws}/workflows``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    steps: list[str] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowStepCreate(BaseModel):
    """Body for ``POST /v1/workspaces/{ws}/workflows/{wf}/steps``.

    ``initial_status`` defaults to ``running`` for backwards compatibility
    (the v0.2 in-process flow where the agent starts work immediately).
    Set to ``pending`` for the durable workflow executor: the step will
    be visible via the ``/pending`` endpoint and a worker can lease it.

    v1.1: ``max_attempts`` / ``retry_policy`` / ``retry_initial_delay_seconds``
    / ``retry_max_delay_seconds`` / ``retry_jitter`` configure the per-step
    retry policy. Defaults preserve v1.0 behaviour: a single attempt with
    no delay (i.e. ``max_attempts=1``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    snapshot_id: str | None = None
    input: Any | None = None
    initial_status: Literal["running", "pending"] = "running"
    # v1.1 — retries
    max_attempts: int = Field(default=1, ge=1, le=100)
    retry_policy: Literal["none", "exponential", "fixed"] = "none"
    retry_initial_delay_seconds: float = Field(default=1.0, ge=0.0, le=3600.0)
    retry_max_delay_seconds: float = Field(default=60.0, ge=0.0, le=86400.0)
    retry_jitter: bool = True


class WorkflowStepUpdate(BaseModel):
    """Body for ``PATCH /v1/workspaces/{ws}/workflows/{wf}/steps/{step_id}``."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["running", "completed", "failed", "cancelled"]
    output: Any | None = None
    error: str | None = None
    snapshot_id: str | None = None


class WorkflowStep(BaseModel):
    """One attempt at one step of a workflow.

    v1.1: ``max_attempts`` / ``retry_policy`` / ``retry_initial_delay_seconds``
    / ``retry_max_delay_seconds`` / ``retry_jitter`` / ``next_retry_at``
    carry the per-step retry policy. Defaults preserve v1.0 behaviour
    (single attempt, no delay). When a step is failing but still has
    attempts remaining, the worker's ``fail_step`` call sets
    ``next_retry_at`` and reverts the row to ``pending`` so a worker can
    pick it up again once the delay elapses.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workflow_id: str
    name: str
    status: WorkflowStepStatus
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input: Any | None = None
    output: Any | None = None
    error: str | None = None
    snapshot_id: str | None = None
    created_at: datetime
    # v1.1 — retries
    max_attempts: int = 1
    retry_policy: Literal["none", "exponential", "fixed"] = "none"
    retry_initial_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 60.0
    retry_jitter: bool = True
    next_retry_at: datetime | None = None


class Workflow(BaseModel):
    """A named, manifest-driven sequence of agent steps."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    name: str
    steps_manifest: list[str]
    steps: list[WorkflowStep] = Field(default_factory=list)
    status: WorkflowStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowList(BaseModel):
    workflows: list[Workflow]


class DLQEntry(BaseModel):
    """A workflow-step that exhausted its retries and landed in the DLQ.

    Mirrors ``CONTRACTS.md → Workflow Retries + Dead-Letter Queue``. Each
    row carries a JSON snapshot of the failing :class:`WorkflowStep` (in
    ``step_snapshot``) so the operator can inspect the exact attempt that
    failed terminally — useful both for debugging and for replay, where
    the snapshot drives the new step's input/snapshot_id.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    step_id: str
    workflow_id: str
    workspace_id: str
    step_name: str
    attempts: int
    last_error: str | None = None
    failed_at: datetime
    step_snapshot: dict[str, Any] = Field(default_factory=dict)


class DLQEntryList(BaseModel):
    """Wrapper for ``GET .../workflows/{wf}/dlq``."""

    model_config = ConfigDict(extra="forbid")

    entries: list[DLQEntry] = Field(default_factory=list)


class DLQReplayResult(BaseModel):
    """Result of ``POST .../workflows/{wf}/dlq/{id}/replay``.

    Returns the new step row created from the snapshot; the DLQ row is
    deleted in the same transaction so the entry never lives in both
    places at once. ``replayed_step`` is ``None`` only when the response
    is being constructed by code paths that handed off the step lookup
    elsewhere (kept optional for forward compatibility).
    """

    model_config = ConfigDict(extra="forbid")

    dlq_id: str
    replayed_step: WorkflowStep | None = None


class ResumeInfo(BaseModel):
    """Resumption state for a workflow."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_status: str
    next_step: str | None = None
    last_completed: WorkflowStep | None = None
    snapshot_id: str | None = None


# ---------------------------------------------------------------------------
# v0.4 — GC + Retention


class RetentionPolicy(BaseModel):
    """How aggressively GC trims a workspace.

    All three "keep" knobs are nullable. ``None`` means "no rule from this
    knob"; the GC engine takes the **union** of the active rules and always
    preserves versions referenced by any non-deleted snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    keep_versions: int | None = None
    keep_days: int | None = None
    keep_snapshots: int | None = None
    delete_unreferenced_blobs: bool = True
    updated_at: datetime


class RetentionPolicyUpdate(BaseModel):
    """Body of ``PUT /v1/workspaces/{ws}/retention``.

    Mirrors :class:`RetentionPolicy` minus the server-managed fields.
    """

    model_config = ConfigDict(extra="forbid")

    keep_versions: int | None = Field(default=None, ge=1)
    keep_days: int | None = Field(default=None, ge=1)
    keep_snapshots: int | None = Field(default=None, ge=1)
    delete_unreferenced_blobs: bool = True


class GCResult(BaseModel):
    """Outcome of one GC pass over a workspace."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    kv_versions_deleted: int = 0
    file_versions_deleted: int = 0
    blob_files_deleted: int = 0
    snapshots_deleted: int = 0
    branches_deleted: int = 0
    bytes_freed: int = 0


class GCResultList(BaseModel):
    results: list[GCResult]


# ---------------------------------------------------------------------------
# v0.5 — Durable workflow executor: leases + workers
# ---------------------------------------------------------------------------


LeaseStatus = Literal["running", "released", "expired"]
WorkerStatus = Literal["active", "draining", "gone"]


class Lease(BaseModel):
    """A soft lock held by a single worker over one workflow step.

    ``status`` reflects only the lease lifecycle, not the step lifecycle.
    The two are coupled at release time: a release with ``status=completed``
    flips both the lease (``released``) and the step (``completed``) in one
    transactional update.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    worker_id: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    status: LeaseStatus = "running"


class WorkerRegistration(BaseModel):
    """Body for ``POST /v1/workers/register``."""

    model_config = ConfigDict(extra="forbid")

    hostname: str | None = None
    pid: int | None = None


class Worker(BaseModel):
    """A registered worker process."""

    model_config = ConfigDict(extra="forbid")

    id: str
    hostname: str | None = None
    pid: int | None = None
    started_at: datetime
    last_heartbeat_at: datetime
    status: WorkerStatus = "active"


class WorkerList(BaseModel):
    workers: list[Worker]


class WorkflowStepList(BaseModel):
    steps: list[WorkflowStep]


class LeaseList(BaseModel):
    leases: list[Lease]


class LeaseAcquireBody(BaseModel):
    """Body for ``POST .../steps/{step_id}/lease``."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    ttl_seconds: int = Field(default=60, ge=1, le=3600)


class LeaseHeartbeatBody(BaseModel):
    """Body for ``POST .../steps/{step_id}/heartbeat``."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    ttl_seconds: int | None = Field(default=None, ge=1, le=3600)


class LeaseReleaseBody(BaseModel):
    """Body for ``POST .../steps/{step_id}/release``.

    ``status`` is the desired *step* status after release. ``completed`` /
    ``failed`` / ``cancelled`` are terminal; ``pending`` re-queues the step.
    """

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    status: Literal["completed", "failed", "cancelled", "pending"] = "completed"
    output: Any | None = None
    error: str | None = None
    snapshot_id: str | None = None


# ---------------------------------------------------------------------------
# v0.6 — Generic resource locks
# ---------------------------------------------------------------------------


class Lock(BaseModel):
    """A generic distributed lock over a named workspace resource.

    Locks are independent of the workflow-step lease primitive; they exist
    so two agents can coordinate access to any named object (KV key, file
    path, external resource handle, etc.) without each one having to
    invent its own protocol.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    workspace_id: str
    holder: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    waiters: int = 0


class LockAcquireBody(BaseModel):
    """Body for ``POST .../locks/{name}/acquire``.

    ``wait_ms == 0`` is fail-fast: the caller gets a 409 immediately if
    the lock is held. Any positive value polls the database every 100ms
    until either the lock is released or the budget elapses.
    """

    model_config = ConfigDict(extra="forbid")

    holder: str = Field(min_length=1)
    ttl_seconds: int = Field(default=60, ge=1, le=86_400)
    wait_ms: int = Field(default=0, ge=0, le=600_000)


class LockHeartbeatBody(BaseModel):
    """Body for ``POST .../locks/{name}/heartbeat``."""

    model_config = ConfigDict(extra="forbid")

    holder: str = Field(min_length=1)
    ttl_seconds: int | None = Field(default=None, ge=1, le=86_400)


class LockReleaseBody(BaseModel):
    """Body for ``POST .../locks/{name}/release``."""

    model_config = ConfigDict(extra="forbid")

    holder: str = Field(min_length=1)


class LockList(BaseModel):
    """Wrapper for ``GET .../locks``."""

    locks: list[Lock]


# ---------------------------------------------------------------------------
# v0.6 — Migration rollback
# ---------------------------------------------------------------------------


class RollbackBody(BaseModel):
    """Body for ``POST /v1/admin/migrations/rollback``."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1)
    dry_run: bool = False


class RolledBackMigrationModel(BaseModel):
    """One migration that was rolled back, with timing info.

    Mirrors the runner's ``RolledBackMigration`` dataclass at the API
    boundary so HTTP clients see ``{id, rolled_back_at, duration_ms}``
    objects in the rollback response.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    rolled_back_at: datetime
    duration_ms: int


class RollbackResult(BaseModel):
    """Outcome of a rollback request.

    ``rolled_back`` lists migrations in execution order (reverse of
    application order). Each entry carries the migration ID, the
    timestamp the rollback completed, and how long the rollback SQL took
    to execute. ``failed`` is set to the first ID that errored
    mid-rollback; everything before it in ``rolled_back`` is committed,
    everything after is unprocessed. ``dry_run`` echoes the request flag
    so clients can confirm they got what they asked for.
    """

    model_config = ConfigDict(extra="forbid")

    target: str
    rolled_back: list[RolledBackMigrationModel] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    failed: str | None = None
    error_message: str | None = None
    dry_run: bool = False
