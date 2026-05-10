# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Pydantic models — wire contract per ``CONTRACTS.md`` (Gateway API)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolRegistration(BaseModel):
    """Body for ``POST /v1/tools/register``."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(..., description="Stable identifier, e.g. 'web.fetch'")
    name: str
    description: str
    transport: Literal["http", "stdio"]
    endpoint: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    idempotent: bool = False
    side_effects: Literal["none", "read", "write"] = "read"
    cache_ttl_seconds: int | None = 300
    auth_method: Literal["none", "bearer", "oauth2"] = "none"
    auth_config: dict[str, Any] = Field(default_factory=dict)


class Tool(ToolRegistration):
    """Full tool record returned by GET routes."""

    created_at: datetime
    updated_at: datetime


class ToolListResponse(BaseModel):
    tools: list[Tool]


# ---------------------------------------------------------------------------
# Invoke
# ---------------------------------------------------------------------------


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str | None = None
    agent_id: str | None = None
    cache: bool = True
    idempotency_key: str | None = None


class InvokeResponse(BaseModel):
    tool_id: str
    arguments: dict[str, Any]
    result: Any
    cached: bool
    duration_ms: int
    audit_id: str
    cost_estimate_usd: float = 0.0


class DryRunResponse(BaseModel):
    tool_id: str
    arguments: dict[str, Any]
    would_invoke: bool
    cached_result: Any | None = None
    estimated_cost_usd: float = 0.0
    estimated_duration_ms: int = 0


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    id: str
    timestamp: datetime
    tool_id: str
    workspace_id: str | None = None
    agent_id: str | None = None
    arguments_hash: str
    arguments_preview: str | None = None
    result_hash: str | None = None
    cached: bool
    duration_ms: int
    cost_estimate_usd: float = 0.0
    error: str | None = None


class AuditListResponse(BaseModel):
    events: list[AuditEvent]


class AuditToolStat(BaseModel):
    tool_id: str
    count: int
    cost: float


class AuditStats(BaseModel):
    total_invocations: int
    cached_count: int
    error_count: int
    total_cost_usd: float
    by_tool: list[AuditToolStat]


class AuditStatsResponse(BaseModel):
    stats: AuditStats


class ChainVerifyResult(BaseModel):
    """Outcome of ``GET /v1/audit/verify`` — tamper-evidence check.

    ``verified`` is True iff every audit_events row in the verification
    window passed the hash-chain check. ``checked`` reports how many
    non-NULL ``event_hash`` rows were actually inspected (legacy rows
    pre-v1.0 are skipped). ``broken_at`` carries the ID of the first
    failing event; ``broken_reason`` is one of ``hash_mismatch``,
    ``prev_hash_mismatch``, ``missing_prev_hash``.
    """

    model_config = ConfigDict(extra="forbid")

    verified: bool
    checked: int = 0
    broken_at: str | None = None
    broken_reason: str | None = None


# ---------------------------------------------------------------------------
# v1.2 — LLM audit recording
# ---------------------------------------------------------------------------


class LLMAuditRecordRequest(BaseModel):
    """Body for ``POST /v1/audit/record-llm``.

    The Plinth Python SDK posts this after every successful direct LLM
    call (i.e. via ``client.llm.complete()`` rather than a registered
    gateway tool). The endpoint synthesises an ``audit_events`` row with
    ``tool_id="llm.<provider>"`` so existing dashboards keying on the
    audit log automatically pick up direct LLM cost too.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    model: str
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    cost_usd: float = Field(0.0, ge=0)
    duration_ms: int = Field(0, ge=0)
    workspace_id: str | None = None
    agent_id: str | None = None
    finish_reason: str | None = None


class LLMAuditRecordResponse(BaseModel):
    """Response for ``POST /v1/audit/record-llm`` — just the new audit ID."""

    model_config = ConfigDict(extra="forbid")

    audit_id: str


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class CacheStats(BaseModel):
    hits: int
    misses: int
    entries: int
    size_bytes: int


# ---------------------------------------------------------------------------
# Tenants (v0.3)
# ---------------------------------------------------------------------------


class Tenant(BaseModel):
    """One tenant visible via the gateway, derived from audit/tool rows."""

    id: str
    audit_count: int = 0
    tool_count: int = 0


class TenantList(BaseModel):
    tenants: list[Tenant]


# ---------------------------------------------------------------------------
# Limits (rate + cost)
# ---------------------------------------------------------------------------


class AgentLimits(BaseModel):
    """Per-agent rate + cost-cap configuration.

    The default values mirror the global defaults in :class:`Settings`. When an
    agent has no override row in ``agent_limits``, the gateway responds with
    the global defaults (with ``agent_id`` filled in).
    """

    agent_id: str
    rpm: int = 60
    burst: int = 20
    cost_cap_usd_hour: float = 1.0
    cost_cap_usd_day: float = 10.0
    updated_at: datetime


class AgentLimitsBody(BaseModel):
    """Body for ``POST /v1/limits/{agent_id}`` — every field is optional.

    Unspecified fields fall back to the global defaults (or to the agent's
    existing row if one is already present).
    """

    model_config = ConfigDict(extra="forbid")

    rpm: int | None = Field(default=None, ge=0)
    burst: int | None = Field(default=None, ge=0)
    cost_cap_usd_hour: float | None = Field(default=None, ge=0)
    cost_cap_usd_day: float | None = Field(default=None, ge=0)


class LimitsStatus(BaseModel):
    """Current usage vs configured caps."""

    agent_id: str
    rpm_limit: int
    rpm_used_in_window: int
    cost_cap_usd_hour: float
    cost_used_usd_hour: float
    cost_cap_usd_day: float
    cost_used_usd_day: float


# ---------------------------------------------------------------------------
# Health & errors
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str
    service: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------------------
# OAuth (v0.3)
# ---------------------------------------------------------------------------


class OAuthConnectionPublic(BaseModel):
    """API-safe view of an OAuth connection — no secret material.

    The encrypted access/refresh tokens are NEVER returned to API callers.
    The gateway looks them up server-side when a tool with
    ``auth_method=oauth2`` is invoked.
    """

    id: str
    tenant_id: str
    provider: str
    user_id: str
    user_login: str | None = None
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None
    last_refreshed_at: datetime | None = None


class OAuthConnectionListResponse(BaseModel):
    connections: list[OAuthConnectionPublic]


class OAuthRefreshRequest(BaseModel):
    """Body for ``POST /v1/oauth/{provider}/refresh``."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str


class OAuthConnectionCreate(BaseModel):
    """Body for ``POST /v1/oauth/connections`` (manual import — for tests/dev).

    Most callers obtain connections via the ``/authorize → /callback`` flow.
    This endpoint exists primarily for tests and ops tooling that need to
    seed a connection from a token already obtained out-of-band.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    user_id: str
    user_login: str | None = None
    scopes: list[str] = Field(default_factory=list)
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    tenant_id: str = "default"


class OAuthRefreshResponse(BaseModel):
    """Response for ``POST /v1/oauth/{provider}/refresh``."""

    connection_id: str
    expires_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    refreshed: bool


# ---------------------------------------------------------------------------
# Transactions (v0.5)
# ---------------------------------------------------------------------------


TransactionStatus = Literal[
    "pending",
    "committing",
    "committed",
    "compensating",
    "rolled_back",
    "failed",
]

TransactionCallStatus = Literal[
    "pending",
    "running",
    "committed",
    "compensating",
    "compensated",
    "failed",
]


class CompensationSpec(BaseModel):
    """Defines how to undo a successful call.

    The ``arguments_template`` may reference the forward call's result via
    ``{result.<field>}`` placeholders — they are substituted at compensation
    time with the value from the forward call's response.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    arguments_template: dict[str, Any] = Field(default_factory=dict)


class TransactionCreate(BaseModel):
    """Body for ``POST /v1/transactions``."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = None
    agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TransactionCallAdd(BaseModel):
    """Body for ``POST /v1/transactions/{tx_id}/calls``."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    compensation: CompensationSpec | None = None


class TransactionCall(BaseModel):
    """A single tool call within a transaction."""

    id: str
    tx_id: str
    seq: int
    tool_id: str
    arguments: dict[str, Any]
    compensation: CompensationSpec | None = None
    status: TransactionCallStatus = "pending"
    result: Any | None = None
    error: str | None = None
    invoked_at: datetime | None = None
    finished_at: datetime | None = None


class Transaction(BaseModel):
    """A grouped sequence of tool calls with optional compensations."""

    id: str
    status: TransactionStatus = "pending"
    workspace_id: str | None = None
    agent_id: str | None = None
    tenant_id: str = "default"
    metadata: dict[str, Any] = Field(default_factory=dict)
    calls: list[TransactionCall] = Field(default_factory=list)
    created_at: datetime
    committed_at: datetime | None = None
    rolled_back_at: datetime | None = None


class TransactionResult(BaseModel):
    """Outcome of a commit or rollback."""

    tx_id: str
    status: TransactionStatus
    calls: list[TransactionCall]
    compensations_run: int = 0


class TransactionListResponse(BaseModel):
    transactions: list[Transaction]


# ---------------------------------------------------------------------------
# v0.6 — Migration rollback
# ---------------------------------------------------------------------------


class RollbackBody(BaseModel):
    """Body for ``POST /v1/admin/migrations/rollback``."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1)
    dry_run: bool = False


class RolledBackMigrationModel(BaseModel):
    """One migration that was rolled back, with timing info."""

    model_config = ConfigDict(extra="forbid")

    id: str
    rolled_back_at: datetime
    duration_ms: int


class RollbackResult(BaseModel):
    """Outcome of a rollback request.

    See :class:`plinth_gateway.migration_runner.RollbackResult` for the
    runner-side counterpart.
    """

    model_config = ConfigDict(extra="forbid")

    target: str
    rolled_back: list[RolledBackMigrationModel] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    failed: str | None = None
    error_message: str | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# v1.4 — Per-agent cost rollup + anomaly detection
# ---------------------------------------------------------------------------


class ToolUsage(BaseModel):
    """One tool's usage breakdown for a single agent.

    Used inside :class:`AgentCost.top_tools` to give dashboards a
    "cost by tool" stack within each agent row.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    invocations: int = 0
    cost_usd: float = 0.0


class AgentCost(BaseModel):
    """Per-agent cost roll-up over a window.

    Bucketing rule: rows with ``agent_id IS NULL`` are grouped under the
    sentinel ``agent_id="(unknown)"`` so the row stays visible without
    leaking the absence-vs-presence distinction into the response shape.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    tenant_id: str
    invocations: int = 0
    cached_invocations: int = 0
    total_cost_usd: float = 0.0
    avg_duration_ms: float = 0.0
    top_tools: list[ToolUsage] = Field(default_factory=list)


class CostByAgentReport(BaseModel):
    """Response of ``GET /v1/audit/cost-by-agent``."""

    model_config = ConfigDict(extra="forbid")

    window: str
    window_start: datetime
    window_end: datetime
    agents: list[AgentCost] = Field(default_factory=list)
    total_agents: int = 0
    total_cost_usd: float = 0.0
    fetched_at: datetime


AnomalyType = Literal[
    "cost_spike",
    "rate_spike",
    "error_spike",
    "new_tool",
    "unusual_pattern",
]
AnomalySeverity = Literal["info", "warning", "critical"]


class Anomaly(BaseModel):
    """A single detected anomaly.

    ``raw_data`` carries detector-specific extras (e.g. the per-minute
    baseline samples used to compute the z-score). The dashboard's
    sparkline reads these straight off the response so detection is the
    single source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: AnomalyType
    severity: AnomalySeverity
    agent_id: str | None = None
    tenant_id: str | None = None
    tool_id: str | None = None
    detected_at: datetime
    window_start: datetime
    window_end: datetime
    description: str
    metric_name: str
    metric_value: float = 0.0
    baseline_mean: float = 0.0
    baseline_stddev: float = 0.0
    z_score: float = 0.0
    raw_data: dict[str, Any] = Field(default_factory=dict)


class AnomalyReport(BaseModel):
    """Response of ``GET /v1/audit/anomalies``."""

    model_config = ConfigDict(extra="forbid")

    detected_at: datetime
    window: str
    anomalies: list[Anomaly] = Field(default_factory=list)
    total_anomalies: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
