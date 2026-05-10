# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Pydantic models mirroring the Plinth API contracts.

These types are the SDK's source of truth for service responses. They
mirror the schema declared in ``/CONTRACTS.md`` and should be kept in
sync with the workspace and gateway services.

Note: Pydantic v2 evaluates field annotations at runtime. We use the
``typing.Optional`` / ``typing.Dict`` style annotations here for
maximum forward-compatibility — the rest of the SDK uses PEP 604
freely. Both styles produce identical types.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional  # noqa: UP035

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Workspace API models
# ---------------------------------------------------------------------------


class Workspace(BaseModel):
    """A top-level isolation boundary for an agent's state."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    tenant_id: str = "default"
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class KVEntry(BaseModel):
    """A single (immutable) version of a key in the versioned KV store."""

    model_config = ConfigDict(extra="ignore")

    workspace_id: str
    key: str
    value: Any
    version: int
    created_at: datetime
    deleted: bool = False
    branch_id: Optional[str] = None  # noqa: UP045


class FileEntry(BaseModel):
    """Metadata describing a stored file version."""

    model_config = ConfigDict(extra="ignore")

    workspace_id: str
    path: str
    size: int
    sha256: str
    content_type: str
    version: int
    created_at: datetime
    deleted: bool = False
    branch_id: Optional[str] = None  # noqa: UP045


class Snapshot(BaseModel):
    """An immutable point-in-time view of a workspace."""

    model_config = ConfigDict(extra="ignore")

    id: str
    workspace_id: str
    name: str
    message: Optional[str] = None  # noqa: UP045
    created_at: datetime
    kv_versions: Dict[str, int] = Field(default_factory=dict)  # noqa: UP006
    file_versions: Dict[str, int] = Field(default_factory=dict)  # noqa: UP006
    parent_snapshot_id: Optional[str] = None  # noqa: UP045


class Branch(BaseModel):
    """A divergent timeline forked from a snapshot."""

    model_config = ConfigDict(extra="ignore")

    id: str
    workspace_id: str
    name: str
    from_snapshot_id: str
    created_at: datetime
    merged: bool = False
    merged_at: Optional[datetime] = None  # noqa: UP045


class DiffResult(BaseModel):
    """Result of comparing two snapshots."""

    model_config = ConfigDict(extra="ignore")

    kv_added: List[str] = Field(default_factory=list)  # noqa: UP006
    kv_modified: List[str] = Field(default_factory=list)  # noqa: UP006
    kv_deleted: List[str] = Field(default_factory=list)  # noqa: UP006
    files_added: List[str] = Field(default_factory=list)  # noqa: UP006
    files_modified: List[str] = Field(default_factory=list)  # noqa: UP006
    files_deleted: List[str] = Field(default_factory=list)  # noqa: UP006


class MergeResult(BaseModel):
    """Result of merging a branch back into main."""

    model_config = ConfigDict(extra="ignore")

    branch_id: str
    workspace_id: str
    merged_at: datetime
    kv_keys_merged: List[str] = Field(default_factory=list)  # noqa: UP006
    file_paths_merged: List[str] = Field(default_factory=list)  # noqa: UP006
    conflicts: List[str] = Field(default_factory=list)  # noqa: UP006


# ---------------------------------------------------------------------------
# Gateway API models
# ---------------------------------------------------------------------------


class ToolRegistration(BaseModel):
    """Payload for registering a tool with the gateway."""

    model_config = ConfigDict(extra="ignore")

    tool_id: str
    name: str
    description: str
    transport: Literal["http", "stdio"]
    endpoint: str
    input_schema: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    output_schema: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    idempotent: bool = False
    side_effects: Literal["none", "read", "write"] = "read"
    cache_ttl_seconds: Optional[int] = 300  # noqa: UP045
    auth_method: Literal["none", "bearer", "oauth2"] = "none"
    auth_config: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class Tool(ToolRegistration):
    """A registered tool, as returned by the gateway."""

    created_at: datetime
    updated_at: datetime


class InvokeRequest(BaseModel):
    """Payload sent to ``POST /v1/invoke``."""

    model_config = ConfigDict(extra="ignore")

    tool_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    workspace_id: Optional[str] = None  # noqa: UP045
    agent_id: Optional[str] = None  # noqa: UP045
    cache: bool = True
    idempotency_key: Optional[str] = None  # noqa: UP045


class InvokeResponse(BaseModel):
    """Response from a successful tool invocation."""

    model_config = ConfigDict(extra="ignore")

    tool_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    result: Any
    cached: bool
    duration_ms: int
    audit_id: str
    cost_estimate_usd: float = 0.0


class DryRunResponse(BaseModel):
    """Response from a dry-run invocation."""

    model_config = ConfigDict(extra="ignore")

    tool_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    would_invoke: bool
    cached_result: Any = None
    estimated_cost_usd: float = 0.0
    estimated_duration_ms: int = 0


class AuditEvent(BaseModel):
    """A single audit log entry for a tool invocation."""

    model_config = ConfigDict(extra="ignore")

    id: str
    timestamp: datetime
    tool_id: str
    workspace_id: Optional[str] = None  # noqa: UP045
    agent_id: Optional[str] = None  # noqa: UP045
    arguments_hash: str
    result_hash: str
    cached: bool
    duration_ms: int
    cost_estimate_usd: float = 0.0
    error: Optional[str] = None  # noqa: UP045


# ---------------------------------------------------------------------------
# v0.2 — Channels API models
# ---------------------------------------------------------------------------


class ChannelMessage(BaseModel):
    """A single message persisted on a workspace channel."""

    model_config = ConfigDict(extra="ignore")

    id: str
    channel: str
    workspace_id: str
    seq: int
    payload: Any = None
    sender: Optional[str] = None  # noqa: UP045
    type: Optional[str] = None  # noqa: UP045
    correlation_id: Optional[str] = None  # noqa: UP045
    headers: Dict[str, str] = Field(default_factory=dict)  # noqa: UP006
    sent_at: datetime
    delivered_at: Optional[datetime] = None  # noqa: UP045


class Channel(BaseModel):
    """Metadata about a workspace channel."""

    model_config = ConfigDict(extra="ignore")

    name: str
    workspace_id: str
    message_count: int = 0
    created_at: datetime
    last_send_at: Optional[datetime] = None  # noqa: UP045
    last_receive_at: Optional[datetime] = None  # noqa: UP045


class ChannelSchema(BaseModel):
    """A JSON Schema attached to a workspace channel (v0.5).

    Carrying a schema turns the channel into a *typed* channel — every
    payload is validated at send time and failures land on a hidden
    ``<channel>.deadletter`` sub-channel. ``version`` increments by 1 on
    each :meth:`ChannelsProxy.set_schema` call.
    """

    # ``schema_`` is a Pydantic-protected namespace; opt out so we can keep
    # the wire field name from CONTRACTS.md.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    workspace_id: str
    channel_name: str
    schema_json: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    version: int = 1
    updated_at: datetime


# ---------------------------------------------------------------------------
# v0.6 — Channel schema migration helpers
# ---------------------------------------------------------------------------


class SchemaCheckResult(BaseModel):
    """Outcome of ``ChannelsProxy.check_schema`` (v0.6).

    Mirrors the workspace service's ``SchemaCheckResult``. ``sample_failures``
    is bounded server-side to 10 entries; each entry has the canonical
    shape ``{"msg_id": str, "errors": [{"path": [...], "message": str}]}``
    so callers don't need shape-detection.
    """

    model_config = ConfigDict(extra="ignore")

    channel: str
    scope: str  # "main" | "deadletter" | "both"
    checked: int = 0
    valid: int = 0
    invalid: int = 0
    sample_failures: List[Dict[str, Any]] = Field(default_factory=list)  # noqa: UP006


class ReplayBatchResult(BaseModel):
    """Outcome of ``ChannelsProxy.replay_all_dlq`` (v0.6)."""

    model_config = ConfigDict(extra="ignore")

    channel: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: List[Dict[str, Any]] = Field(default_factory=list)  # noqa: UP006
    dry_run: bool = False


# ---------------------------------------------------------------------------
# v0.2 — Workflows API models
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """A single step in a workflow's log.

    v1.1 adds the optional retry-policy fields. Defaults preserve v1.0
    behaviour (single attempt, no retry delay) so v1.0 servers' rows
    deserialise cleanly.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    workflow_id: str
    name: str
    status: str  # "pending" | "running" | "completed" | "failed" | "cancelled"
    attempt: int = 1
    started_at: Optional[datetime] = None  # noqa: UP045
    finished_at: Optional[datetime] = None  # noqa: UP045
    input: Any = None
    output: Any = None
    error: Optional[str] = None  # noqa: UP045
    snapshot_id: Optional[str] = None  # noqa: UP045
    created_at: Optional[datetime] = None  # noqa: UP045
    # v1.1 — retries
    max_attempts: int = 1
    retry_policy: str = "none"  # "none" | "exponential" | "fixed"
    retry_initial_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 60.0
    retry_jitter: bool = True
    next_retry_at: Optional[datetime] = None  # noqa: UP045


class DLQEntry(BaseModel):
    """A workflow step that exhausted ``max_attempts`` and landed in the DLQ.

    Mirrors :class:`plinth_workspace.models.DLQEntry`. ``step_snapshot``
    is the JSON-decoded view of the step row at failure time so the
    operator can inspect the exact attempt that failed terminally —
    useful both for debugging and for replay, where the snapshot drives
    the new step's input/snapshot_id.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    step_id: str
    workflow_id: str
    workspace_id: str
    step_name: str
    attempts: int
    last_error: Optional[str] = None  # noqa: UP045
    failed_at: datetime
    step_snapshot: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class Workflow(BaseModel):
    """A workflow: a manifest of expected steps + a log of completed ones."""

    model_config = ConfigDict(extra="ignore")

    id: str
    workspace_id: str
    name: str
    steps_manifest: List[str] = Field(default_factory=list)  # noqa: UP006
    steps: List[WorkflowStep] = Field(default_factory=list)  # noqa: UP006
    status: str  # "pending" | "running" | "completed" | "failed" | "cancelled"
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    created_at: datetime
    started_at: Optional[datetime] = None  # noqa: UP045
    finished_at: Optional[datetime] = None  # noqa: UP045


class ResumeInfo(BaseModel):
    """The information returned by ``GET /workflows/{id}/resume``."""

    model_config = ConfigDict(extra="ignore")

    workflow_id: str
    workflow_status: str
    next_step: Optional[str] = None  # noqa: UP045
    last_completed: Optional[WorkflowStep] = None  # noqa: UP045
    snapshot_id: Optional[str] = None  # noqa: UP045


# ---------------------------------------------------------------------------
# v0.2 — Rate limit / cost cap models (gateway service)
# ---------------------------------------------------------------------------


class AgentLimits(BaseModel):
    """Per-agent rate limit and cost cap configuration."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    rpm: int = 60
    burst: int = 20
    cost_cap_usd_hour: float = 1.0
    cost_cap_usd_day: float = 10.0
    updated_at: datetime


class LimitsStatus(BaseModel):
    """Current usage and configured limits for an agent."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    rpm_limit: int
    rpm_used_in_window: int
    cost_cap_usd_hour: float
    cost_used_usd_hour: float
    cost_cap_usd_day: float
    cost_used_usd_day: float


# ---------------------------------------------------------------------------
# v0.3 — Tenants
# ---------------------------------------------------------------------------


class Tenant(BaseModel):
    """A tenant — top-level isolation boundary across services.

    The workspace + gateway expose a thin ``GET /v1/tenants`` view that
    returns only the tenant id and the workspace count for now (since the
    other services don't own tenant metadata; the identity service does).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: Optional[str] = None  # noqa: UP045
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    workspace_count: Optional[int] = None  # noqa: UP045
    created_at: Optional[datetime] = None  # noqa: UP045


# ---------------------------------------------------------------------------
# v1.0 — Per-tenant resource quotas
# ---------------------------------------------------------------------------


class TenantQuotas(BaseModel):
    """Quota envelope for a single tenant.

    Mirrors the identity service's ``GET /v1/tenants/{id}/quotas`` response.
    Defaults match :doc:`/CONTRACTS.md` so callers can construct partial
    overrides via :meth:`IdentityClient.set_quotas` without restating
    every field.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    max_workspaces: int = 100
    max_storage_gb: float = 10.0
    max_channels_per_workspace: int = 50
    max_workflows_per_workspace: int = 100
    max_active_tokens: int = 1000
    max_oauth_connections: int = 50
    max_cost_usd_day: float = 100.0
    max_cost_usd_month: float = 2000.0
    max_invocations_per_minute: int = 600
    updated_at: Optional[datetime] = None  # noqa: UP045


class TenantQuotasUpdate(BaseModel):
    """Partial-update body for ``POST /v1/tenants/{id}/quotas``.

    All fields optional — unset values fall back to the existing row (or
    the contract defaults if no row exists).
    """

    model_config = ConfigDict(extra="forbid")

    max_workspaces: Optional[int] = None  # noqa: UP045
    max_storage_gb: Optional[float] = None  # noqa: UP045
    max_channels_per_workspace: Optional[int] = None  # noqa: UP045
    max_workflows_per_workspace: Optional[int] = None  # noqa: UP045
    max_active_tokens: Optional[int] = None  # noqa: UP045
    max_oauth_connections: Optional[int] = None  # noqa: UP045
    max_cost_usd_day: Optional[float] = None  # noqa: UP045
    max_cost_usd_month: Optional[float] = None  # noqa: UP045
    max_invocations_per_minute: Optional[int] = None  # noqa: UP045


class TenantUsage(BaseModel):
    """Computed usage rollup from ``GET /v1/tenants/{id}/usage``.

    Cross-service fields (``storage_gb``, ``cost_usd_day``,
    ``cost_usd_month``, ``last_invocation_at``, ``workspaces``,
    ``oauth_connections``) are reported as ``0`` / ``None`` with a
    ``notes`` map pointing at the canonical source — v1.0 doesn't
    aggregate cross-service usage at the identity layer (known
    limitation).
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    workspaces: int = 0
    storage_gb: float = 0.0
    active_tokens: int = 0
    oauth_connections: int = 0
    cost_usd_day: float = 0.0
    cost_usd_month: float = 0.0
    last_invocation_at: Optional[datetime] = None  # noqa: UP045
    notes: Dict[str, str] = Field(default_factory=dict)  # noqa: UP006


# ---------------------------------------------------------------------------
# v0.4 — Identity signing keys
# ---------------------------------------------------------------------------


class SigningKey(BaseModel):
    """Public-safe view of an RS256 signing key.

    Mirrors the identity service's ``GET /v1/keys`` response item.
    Never carries private key material.
    """

    model_config = ConfigDict(extra="ignore")

    kid: str
    alg: str
    public_key_pem: str
    created_at: datetime
    rotated_in_at: Optional[datetime] = None  # noqa: UP045
    expires_at: datetime
    active: bool = False


# ---------------------------------------------------------------------------
# v0.6 — Federated revocation list (cross-replica propagation)
# ---------------------------------------------------------------------------


class RevocationEntry(BaseModel):
    """A single revoked token surfaced by ``GET /v1/revocations``.

    Mirrors the identity service's response item. Carries only the
    metadata downstream caches need to record + audit a revocation; the
    JWT itself is never carried (revocation is keyed by ``jti``).
    """

    model_config = ConfigDict(extra="ignore")

    jti: str
    revoked_at: datetime
    agent_id: str
    tenant_id: str


class RevocationList(BaseModel):
    """Response from ``GET /v1/revocations``.

    Callers maintain a cursor (``next_since``, a unix-second timestamp)
    and re-poll periodically. The server returns at most ``limit``
    entries; ``has_more`` signals an immediately-available next page.
    """

    model_config = ConfigDict(extra="ignore")

    revocations: List[RevocationEntry] = Field(default_factory=list)  # noqa: UP006
    next_since: int = 0
    has_more: bool = False


# ---------------------------------------------------------------------------
# v1.0 — GDPR compliance (export + delete) + audit-chain verify
# ---------------------------------------------------------------------------


class ExportStatus(BaseModel):
    """A snapshot of a tenant data-export job."""

    model_config = ConfigDict(extra="ignore")

    export_id: str
    tenant_id: str
    status: str
    requested_at: datetime
    completed_at: Optional[datetime] = None  # noqa: UP045
    expires_at: Optional[datetime] = None  # noqa: UP045
    size_bytes: Optional[int] = None  # noqa: UP045
    error: Optional[str] = None  # noqa: UP045


class ExportJob(BaseModel):
    """Returned from the initial ``POST /v1/tenants/{id}/export``."""

    model_config = ConfigDict(extra="ignore")

    export_id: str
    status: str = "pending"


class DeleteConfirmation(BaseModel):
    """Returned from ``POST /v1/tenants/{id}/delete-data-confirm``."""

    model_config = ConfigDict(extra="ignore")

    confirm_token: str
    expires_at: datetime


class DeleteJob(BaseModel):
    """A GDPR Article 17 erasure job, polled until ``status`` settles."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    tenant_id: str
    status: str
    requested_at: datetime
    completed_at: Optional[datetime] = None  # noqa: UP045
    deleted_counts: Dict[str, int] = Field(default_factory=dict)  # noqa: UP006
    error: Optional[str] = None  # noqa: UP045


class ChainVerifyResult(BaseModel):
    """Outcome of ``GET /v1/audit/verify`` (gateway tamper-evidence check)."""

    model_config = ConfigDict(extra="ignore")

    verified: bool
    checked: int = 0
    broken_at: Optional[str] = None  # noqa: UP045
    broken_reason: Optional[str] = None  # noqa: UP045


# ---------------------------------------------------------------------------
# v0.5 — Durable workflow executor: leases + workers
# ---------------------------------------------------------------------------


class Lease(BaseModel):
    """A soft lock held by a worker over a single workflow step."""

    model_config = ConfigDict(extra="ignore")

    step_id: str
    worker_id: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    status: str = "running"


class Worker(BaseModel):
    """A registered worker process.

    The ``id`` is server-assigned at registration time. ``status`` is
    one of ``active`` | ``draining`` | ``gone``; the lease reaper is
    responsible for sweeping ``active → gone`` on heartbeat lapse.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    hostname: Optional[str] = None  # noqa: UP045
    pid: Optional[int] = None  # noqa: UP045
    started_at: datetime
    last_heartbeat_at: datetime
    status: str = "active"


# ---------------------------------------------------------------------------
# v0.5 — Workflow Transactions (Saga commit/compensate over tool calls)
# ---------------------------------------------------------------------------


class CompensationSpec(BaseModel):
    """How to undo a successful tool call.

    The ``arguments_template`` may reference the forward call's result via
    ``{result.<field>}`` placeholders, which the gateway substitutes
    server-side at compensation time. It may also reference any prior
    committed call via ``{seq.N.result.<field>}``.
    """

    model_config = ConfigDict(extra="ignore")

    tool_id: str
    arguments_template: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class TransactionCall(BaseModel):
    """A single tool call within a transaction."""

    model_config = ConfigDict(extra="ignore")

    id: str
    tx_id: str
    seq: int
    tool_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    compensation: Optional[CompensationSpec] = None  # noqa: UP045
    status: str = "pending"
    result: Any = None
    error: Optional[str] = None  # noqa: UP045
    invoked_at: Optional[datetime] = None  # noqa: UP045
    finished_at: Optional[datetime] = None  # noqa: UP045


class Transaction(BaseModel):
    """A grouped sequence of tool calls with optional compensations."""

    model_config = ConfigDict(extra="ignore")

    id: str
    status: str = "pending"
    workspace_id: Optional[str] = None  # noqa: UP045
    agent_id: Optional[str] = None  # noqa: UP045
    tenant_id: str = "default"
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    calls: List[TransactionCall] = Field(default_factory=list)  # noqa: UP006
    created_at: datetime
    committed_at: Optional[datetime] = None  # noqa: UP045
    rolled_back_at: Optional[datetime] = None  # noqa: UP045


class TransactionResult(BaseModel):
    """Outcome of a commit / rollback."""

    model_config = ConfigDict(extra="ignore")

    tx_id: str
    status: str
    calls: List[TransactionCall] = Field(default_factory=list)  # noqa: UP006
    compensations_run: int = 0


# ---------------------------------------------------------------------------
# v0.6 — Generic resource locks
# ---------------------------------------------------------------------------


class Lock(BaseModel):
    """A generic distributed lock over a named workspace resource.

    Locks are independent of the workflow-step :class:`Lease` primitive;
    they exist so two agents can coordinate access to any named object
    (KV key, file path, external resource handle, etc.) without having to
    invent their own protocol.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    workspace_id: str
    holder: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    waiters: int = 0


# ---------------------------------------------------------------------------
# v1.2 — LLM layer models
# ---------------------------------------------------------------------------


class LLMMessage(BaseModel):
    """A single message in an LLM conversation.

    Mirrors the OpenAI-style chat schema. Each provider adapter is
    responsible for translating into its native message shape (e.g.
    Anthropic splits the system prompt out of the messages array).
    """

    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: Optional[str] = None  # noqa: UP045
    tool_call_id: Optional[str] = None  # noqa: UP045


class LLMResponse(BaseModel):
    """The result of a non-streaming LLM completion.

    The provider-specific raw response is preserved on ``raw`` so callers
    that need provider-only fields (e.g. structured tool calls, system
    fingerprints) can reach for them.
    """

    model_config = ConfigDict(extra="ignore")

    content: str
    model: str
    finish_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    provider: str
    audit_id: Optional[str] = None  # noqa: UP045
    raw: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class LLMStreamChunk(BaseModel):
    """A single chunk from an LLM streaming response.

    ``delta`` contains incremental text. The final chunk emitted by the
    Plinth wrapper carries ``finish_reason`` so callers can stop
    iterating without having to inspect ``raw``.
    """

    model_config = ConfigDict(extra="ignore")

    delta: str = ""
    finish_reason: Optional[str] = None  # noqa: UP045
    raw: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


__all__ = [
    "AgentLimits",
    "AuditEvent",
    "Branch",
    "Channel",
    "ChannelMessage",
    "ChannelSchema",
    "CompensationSpec",
    "DLQEntry",
    "DiffResult",
    "DryRunResponse",
    "FileEntry",
    "InvokeRequest",
    "InvokeResponse",
    "KVEntry",
    "LLMMessage",
    "LLMResponse",
    "LLMStreamChunk",
    "Lease",
    "LimitsStatus",
    "Lock",
    "MergeResult",
    "ReplayBatchResult",
    "ResumeInfo",
    "RevocationEntry",
    "RevocationList",
    "SchemaCheckResult",
    "SigningKey",
    "Snapshot",
    "Tenant",
    "TenantQuotas",
    "TenantQuotasUpdate",
    "TenantUsage",
    "Tool",
    "ToolRegistration",
    "Transaction",
    "TransactionCall",
    "TransactionResult",
    "Worker",
    "Workflow",
    "WorkflowStep",
    "Workspace",
]
