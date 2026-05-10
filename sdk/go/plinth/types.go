// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import "time"

// Types in this file mirror the Pydantic models in CONTRACTS.md and the
// TypeScript SDK's types.ts. Field names use Go conventions
// (CamelCase) while JSON tags retain the wire snake_case.
//
// Optional fields use either pointer types (string, int) or omitempty
// (slices, maps) so callers can distinguish "absent" from "zero value"
// where the contract relies on it.

// ---------------------------------------------------------------------------
// Workspace surface (workspace service)
// ---------------------------------------------------------------------------

// Workspace is the top-level isolation boundary for an agent's state.
type Workspace struct {
	ID        string         `json:"id"`
	Name      string         `json:"name"`
	CreatedAt time.Time      `json:"created_at"`
	UpdatedAt time.Time      `json:"updated_at"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// KVEntry is a single versioned KV row.
type KVEntry struct {
	WorkspaceID string    `json:"workspace_id"`
	Key         string    `json:"key"`
	Value       any       `json:"value"`
	Version     int       `json:"version"`
	CreatedAt   time.Time `json:"created_at"`
	Deleted     bool      `json:"deleted"`
	BranchID    *string   `json:"branch_id,omitempty"`
}

// FileEntry is metadata for a versioned file (blob bytes are fetched
// separately via FilesProxy.Read).
type FileEntry struct {
	WorkspaceID string    `json:"workspace_id"`
	Path        string    `json:"path"`
	Size        int64     `json:"size"`
	SHA256      string    `json:"sha256"`
	ContentType string    `json:"content_type"`
	Version     int       `json:"version"`
	CreatedAt   time.Time `json:"created_at"`
	Deleted     bool      `json:"deleted"`
	BranchID    *string   `json:"branch_id,omitempty"`
}

// Snapshot captures the latest version of every key/file in a workspace.
type Snapshot struct {
	ID               string         `json:"id"`
	WorkspaceID      string         `json:"workspace_id"`
	Name             string         `json:"name"`
	Message          *string        `json:"message,omitempty"`
	CreatedAt        time.Time      `json:"created_at"`
	KVVersions       map[string]int `json:"kv_versions"`
	FileVersions     map[string]int `json:"file_versions"`
	ParentSnapshotID *string        `json:"parent_snapshot_id,omitempty"`
}

// Branch is a divergent timeline anchored to a snapshot.
type Branch struct {
	ID             string     `json:"id"`
	WorkspaceID    string     `json:"workspace_id"`
	Name           string     `json:"name"`
	FromSnapshotID string     `json:"from_snapshot_id"`
	CreatedAt      time.Time  `json:"created_at"`
	Merged         bool       `json:"merged"`
	MergedAt       *time.Time `json:"merged_at,omitempty"`
}

// DiffResult is the diff between two snapshots.
type DiffResult struct {
	KVAdded        []string `json:"kv_added"`
	KVModified     []string `json:"kv_modified"`
	KVDeleted      []string `json:"kv_deleted"`
	FilesAdded     []string `json:"files_added"`
	FilesModified  []string `json:"files_modified"`
	FilesDeleted   []string `json:"files_deleted"`
}

// MergeResult is returned by Workspace.Merge.
type MergeResult struct {
	BranchID         string    `json:"branch_id"`
	WorkspaceID      string    `json:"workspace_id,omitempty"`
	MergedAt         time.Time `json:"merged_at"`
	Merged           bool      `json:"merged,omitempty"`
	KVKeysMerged     []string  `json:"kv_keys_merged,omitempty"`
	FilePathsMerged  []string  `json:"file_paths_merged,omitempty"`
	Conflicts        []string  `json:"conflicts"`
}

// ---------------------------------------------------------------------------
// Channels (workspace service v0.2)
// ---------------------------------------------------------------------------

// ChannelMessage is a single persisted message on a workspace channel.
type ChannelMessage struct {
	ID            string            `json:"id"`
	Channel       string            `json:"channel"`
	WorkspaceID   string            `json:"workspace_id"`
	Seq           int64             `json:"seq"`
	Payload       any               `json:"payload"`
	Sender        *string           `json:"sender,omitempty"`
	Type          *string           `json:"type,omitempty"`
	CorrelationID *string           `json:"correlation_id,omitempty"`
	Headers       map[string]string `json:"headers,omitempty"`
	SentAt        time.Time         `json:"sent_at"`
	DeliveredAt   *time.Time        `json:"delivered_at,omitempty"`
}

// Channel is metadata about a workspace channel.
type Channel struct {
	Name          string     `json:"name"`
	WorkspaceID   string     `json:"workspace_id"`
	MessageCount  int        `json:"message_count"`
	CreatedAt     time.Time  `json:"created_at"`
	LastSendAt    *time.Time `json:"last_send_at,omitempty"`
	LastReceiveAt *time.Time `json:"last_receive_at,omitempty"`
}

// ---------------------------------------------------------------------------
// Workflows (workspace service v0.2)
// ---------------------------------------------------------------------------

// WorkflowStatus is one of the lifecycle states a workflow / step can
// be in. Values match the server enum exactly.
type WorkflowStatus string

const (
	WorkflowStatusPending   WorkflowStatus = "pending"
	WorkflowStatusRunning   WorkflowStatus = "running"
	WorkflowStatusCompleted WorkflowStatus = "completed"
	WorkflowStatusFailed    WorkflowStatus = "failed"
	WorkflowStatusCancelled WorkflowStatus = "cancelled"
)

// WorkflowStep is a single step in a workflow's log.
type WorkflowStep struct {
	ID         string         `json:"id"`
	WorkflowID string         `json:"workflow_id"`
	Name       string         `json:"name"`
	Status     WorkflowStatus `json:"status"`
	Attempt    int            `json:"attempt"`
	StartedAt  *time.Time     `json:"started_at,omitempty"`
	FinishedAt *time.Time     `json:"finished_at,omitempty"`
	Input      any            `json:"input,omitempty"`
	Output     any            `json:"output,omitempty"`
	Error      *string        `json:"error,omitempty"`
	SnapshotID *string        `json:"snapshot_id,omitempty"`
	CreatedAt  *time.Time     `json:"created_at,omitempty"`
}

// Workflow is a manifest of expected steps + a log of completed ones.
type Workflow struct {
	ID            string         `json:"id"`
	WorkspaceID   string         `json:"workspace_id"`
	Name          string         `json:"name"`
	StepsManifest []string       `json:"steps_manifest"`
	Steps         []WorkflowStep `json:"steps"`
	Status        WorkflowStatus `json:"status"`
	Metadata      map[string]any `json:"metadata,omitempty"`
	CreatedAt     time.Time      `json:"created_at"`
	StartedAt     *time.Time     `json:"started_at,omitempty"`
	FinishedAt    *time.Time     `json:"finished_at,omitempty"`
}

// ResumeInfo is the response from GET /workflows/{id}/resume.
type ResumeInfo struct {
	WorkflowID     string         `json:"workflow_id"`
	WorkflowStatus WorkflowStatus `json:"workflow_status"`
	NextStep       *string        `json:"next_step,omitempty"`
	LastCompleted  *WorkflowStep  `json:"last_completed,omitempty"`
	SnapshotID     *string        `json:"snapshot_id,omitempty"`
}

// ---------------------------------------------------------------------------
// Durable workflow executor (workspace service v0.5)
// ---------------------------------------------------------------------------

// LeaseStatus is the lifecycle for a workflow-step lease.
type LeaseStatus string

const (
	LeaseStatusRunning  LeaseStatus = "running"
	LeaseStatusReleased LeaseStatus = "released"
	LeaseStatusExpired  LeaseStatus = "expired"
)

// Lease is a soft-lock held by a worker over a single workflow step.
type Lease struct {
	StepID      string      `json:"step_id"`
	WorkerID    string      `json:"worker_id"`
	AcquiredAt  time.Time   `json:"acquired_at"`
	ExpiresAt   time.Time   `json:"expires_at"`
	HeartbeatAt time.Time   `json:"heartbeat_at"`
	Status      LeaseStatus `json:"status"`
}

// WorkerStatus is the lifecycle for a worker process.
type WorkerStatus string

const (
	WorkerStatusActive   WorkerStatus = "active"
	WorkerStatusDraining WorkerStatus = "draining"
	WorkerStatusGone     WorkerStatus = "gone"
)

// Worker is a registered worker process.
type Worker struct {
	ID              string       `json:"id"`
	Hostname        *string      `json:"hostname,omitempty"`
	PID             *int         `json:"pid,omitempty"`
	StartedAt       time.Time    `json:"started_at"`
	LastHeartbeatAt time.Time    `json:"last_heartbeat_at"`
	Status          WorkerStatus `json:"status"`
}

// WorkerRegistration is the body sent to POST /v1/workers/register.
type WorkerRegistration struct {
	Hostname *string `json:"hostname,omitempty"`
	PID      *int    `json:"pid,omitempty"`
}

// ---------------------------------------------------------------------------
// Tools / Gateway (gateway service)
// ---------------------------------------------------------------------------

// ToolTransport is the transport mode for a registered tool.
type ToolTransport string

const (
	ToolTransportHTTP  ToolTransport = "http"
	ToolTransportStdio ToolTransport = "stdio"
)

// ToolSideEffects classifies the effect class of a tool's invocation.
type ToolSideEffects string

const (
	ToolSideEffectsNone  ToolSideEffects = "none"
	ToolSideEffectsRead  ToolSideEffects = "read"
	ToolSideEffectsWrite ToolSideEffects = "write"
)

// ToolAuthMethod is the authentication mode for a registered tool.
type ToolAuthMethod string

const (
	ToolAuthNone   ToolAuthMethod = "none"
	ToolAuthBearer ToolAuthMethod = "bearer"
	ToolAuthOAuth2 ToolAuthMethod = "oauth2"
)

// ToolRegistration is the body for POST /v1/tools/register.
type ToolRegistration struct {
	ToolID          string          `json:"tool_id"`
	Name            string          `json:"name"`
	Description     string          `json:"description"`
	Transport       ToolTransport   `json:"transport"`
	Endpoint        string          `json:"endpoint"`
	InputSchema     map[string]any  `json:"input_schema"`
	OutputSchema    map[string]any  `json:"output_schema"`
	Idempotent      bool            `json:"idempotent,omitempty"`
	SideEffects     ToolSideEffects `json:"side_effects,omitempty"`
	CacheTTLSeconds *int            `json:"cache_ttl_seconds,omitempty"`
	AuthMethod      ToolAuthMethod  `json:"auth_method,omitempty"`
	AuthConfig      map[string]any  `json:"auth_config,omitempty"`
}

// Tool is a registered tool as returned by the gateway.
type Tool struct {
	ToolRegistration
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// InvokeRequest is the body for POST /v1/invoke.
type InvokeRequest struct {
	ToolID         string         `json:"tool_id"`
	Arguments      map[string]any `json:"arguments"`
	WorkspaceID    *string        `json:"workspace_id,omitempty"`
	AgentID        *string        `json:"agent_id,omitempty"`
	Cache          *bool          `json:"cache,omitempty"`
	IdempotencyKey *string        `json:"idempotency_key,omitempty"`
}

// InvokeResponse is the response from POST /v1/invoke.
type InvokeResponse struct {
	ToolID          string         `json:"tool_id"`
	Arguments       map[string]any `json:"arguments"`
	Result          any            `json:"result"`
	Cached          bool           `json:"cached"`
	DurationMs      int            `json:"duration_ms"`
	AuditID         string         `json:"audit_id"`
	CostEstimateUSD float64        `json:"cost_estimate_usd"`
}

// DryRunResponse is the response from POST /v1/invoke/dry-run.
type DryRunResponse struct {
	ToolID              string         `json:"tool_id"`
	Arguments           map[string]any `json:"arguments"`
	WouldInvoke         bool           `json:"would_invoke"`
	CachedResult        any            `json:"cached_result,omitempty"`
	EstimatedCostUSD    float64        `json:"estimated_cost_usd"`
	EstimatedDurationMs int            `json:"estimated_duration_ms"`
}

// AuditEvent is a single row from the gateway audit log.
type AuditEvent struct {
	ID              string    `json:"id"`
	Timestamp       time.Time `json:"timestamp"`
	ToolID          string    `json:"tool_id"`
	WorkspaceID     *string   `json:"workspace_id,omitempty"`
	AgentID         *string   `json:"agent_id,omitempty"`
	ArgumentsHash   string    `json:"arguments_hash"`
	ResultHash      string    `json:"result_hash"`
	Cached          bool      `json:"cached"`
	DurationMs      int       `json:"duration_ms"`
	CostEstimateUSD float64   `json:"cost_estimate_usd"`
	Error           *string   `json:"error,omitempty"`
}

// AuditQuery is the parameter struct for ToolGateway.Audit.
type AuditQuery struct {
	WorkspaceID string
	ToolID      string
	Since       string
	Limit       int
}

// ---------------------------------------------------------------------------
// Identity (identity service v0.3)
// ---------------------------------------------------------------------------

// TokenIssueRequest is the body for POST /v1/tokens.
type TokenIssueRequest struct {
	AgentID     string         `json:"agent_id"`
	TenantID    string         `json:"tenant_id,omitempty"`
	Scopes      []string       `json:"scopes"`
	WorkspaceID *string        `json:"workspace_id,omitempty"`
	TTLSeconds  int            `json:"ttl_seconds,omitempty"`
	Metadata    map[string]any `json:"metadata,omitempty"`
}

// TokenClaims is the decoded JWT payload for a Plinth capability token.
type TokenClaims struct {
	Sub         string         `json:"sub"`
	Iss         string         `json:"iss"`
	Aud         string         `json:"aud"`
	Iat         int64          `json:"iat"`
	Exp         int64          `json:"exp"`
	JTI         string         `json:"jti"`
	AgentID     string         `json:"agent_id"`
	TenantID    string         `json:"tenant_id"`
	WorkspaceID *string        `json:"workspace_id,omitempty"`
	Scopes      []string       `json:"scopes"`
	RateLimit   map[string]any `json:"rate_limit,omitempty"`
}

// TokenIssueResponse is returned by POST /v1/tokens.
type TokenIssueResponse struct {
	Token     string      `json:"token"`
	JTI       string      `json:"jti"`
	ExpiresAt time.Time   `json:"expires_at"`
	Claims    TokenClaims `json:"claims"`
}

// TokenInfo is the public-safe view of a token (no secret).
type TokenInfo struct {
	JTI       string         `json:"jti"`
	AgentID   string         `json:"agent_id"`
	TenantID  string         `json:"tenant_id"`
	IssuedAt  time.Time      `json:"issued_at"`
	ExpiresAt time.Time      `json:"expires_at"`
	Revoked   bool           `json:"revoked"`
	RevokedAt *time.Time     `json:"revoked_at,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// SigningKey is the public-safe view of an RS256 signing key.
type SigningKey struct {
	KID          string     `json:"kid"`
	Alg          string     `json:"alg"`
	PublicKeyPEM string     `json:"public_key_pem"`
	CreatedAt    time.Time  `json:"created_at"`
	RotatedInAt  *time.Time `json:"rotated_in_at,omitempty"`
	ExpiresAt    time.Time  `json:"expires_at"`
	Active       bool       `json:"active"`
}

// ---------------------------------------------------------------------------
// Quotas (identity service v1.0)
// ---------------------------------------------------------------------------

// TenantQuotas is the per-tenant quota envelope.
type TenantQuotas struct {
	TenantID                 string     `json:"tenant_id"`
	MaxWorkspaces            int        `json:"max_workspaces"`
	MaxStorageGB             float64    `json:"max_storage_gb"`
	MaxChannelsPerWorkspace  int        `json:"max_channels_per_workspace"`
	MaxWorkflowsPerWorkspace int        `json:"max_workflows_per_workspace"`
	MaxActiveTokens          int        `json:"max_active_tokens"`
	MaxOAuthConnections      int        `json:"max_oauth_connections"`
	MaxCostUSDDay            float64    `json:"max_cost_usd_day"`
	MaxCostUSDMonth          float64    `json:"max_cost_usd_month"`
	MaxInvocationsPerMinute  int        `json:"max_invocations_per_minute"`
	UpdatedAt                *time.Time `json:"updated_at,omitempty"`
}

// TenantUsage is the per-tenant usage rollup.
type TenantUsage struct {
	TenantID          string            `json:"tenant_id"`
	Workspaces        int               `json:"workspaces"`
	StorageGB         float64           `json:"storage_gb"`
	ActiveTokens      int               `json:"active_tokens"`
	OAuthConnections  int               `json:"oauth_connections"`
	CostUSDDay        float64           `json:"cost_usd_day"`
	CostUSDMonth      float64           `json:"cost_usd_month"`
	LastInvocationAt  *time.Time        `json:"last_invocation_at,omitempty"`
	Notes             map[string]string `json:"notes,omitempty"`
}

// ---------------------------------------------------------------------------
// Locks (workspace service v0.6)
// ---------------------------------------------------------------------------

// Lock is a generic distributed lock over a named workspace resource.
type Lock struct {
	Name        string    `json:"name"`
	WorkspaceID string    `json:"workspace_id"`
	Holder      string    `json:"holder"`
	AcquiredAt  time.Time `json:"acquired_at"`
	ExpiresAt   time.Time `json:"expires_at"`
	HeartbeatAt time.Time `json:"heartbeat_at"`
	Waiters     int       `json:"waiters"`
}

// ---------------------------------------------------------------------------
// Rate limits (gateway service v0.2)
// ---------------------------------------------------------------------------

// AgentLimits is the per-agent rate-limit / cost-cap config.
type AgentLimits struct {
	AgentID       string    `json:"agent_id"`
	RPM           int       `json:"rpm"`
	Burst         int       `json:"burst"`
	CostCapUSDHr  float64   `json:"cost_cap_usd_hour"`
	CostCapUSDDay float64   `json:"cost_cap_usd_day"`
	UpdatedAt     time.Time `json:"updated_at"`
}
