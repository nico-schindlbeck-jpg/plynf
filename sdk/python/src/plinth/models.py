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
# v0.2 — Workflows API models
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """A single step in a workflow's log."""

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


__all__ = [
    "AgentLimits",
    "AuditEvent",
    "Branch",
    "Channel",
    "ChannelMessage",
    "ChannelSchema",
    "CompensationSpec",
    "DiffResult",
    "DryRunResponse",
    "FileEntry",
    "InvokeRequest",
    "InvokeResponse",
    "KVEntry",
    "Lease",
    "LimitsStatus",
    "MergeResult",
    "ResumeInfo",
    "SigningKey",
    "Snapshot",
    "Tenant",
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
