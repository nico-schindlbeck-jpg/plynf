// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"net/url"
	"strconv"
)

// InvokeOpts is the optional metadata attached to a tool invocation.
type InvokeOpts struct {
	// WorkspaceID is propagated to the audit log for attribution.
	WorkspaceID string
	// AgentID is propagated to the audit log for attribution.
	AgentID string
	// Cache, when set to false, disables caching for this single call.
	// Use a pointer so the zero value is "unset" (cache enabled).
	Cache *bool
	// IdempotencyKey is a dedup key for at-least-once retry semantics.
	IdempotencyKey string
}

// ToolGateway is the gateway service's tool surface. Reachable via
// *Plinth.Tools.
type ToolGateway struct {
	http *HTTPClient
}

// NewToolGateway constructs a ToolGateway bound to a gateway HTTPClient.
// Exported so tests can wire one up directly without going through *Plinth.
func NewToolGateway(http *HTTPClient) *ToolGateway {
	return &ToolGateway{http: http}
}

// Invoke calls toolID with the given arguments via the gateway. The
// gateway transparently caches/audits per CONTRACTS.md.
func (t *ToolGateway) Invoke(ctx context.Context, toolID string, args map[string]any, opts InvokeOpts) (*InvokeResponse, error) {
	body := t.invokeBody(toolID, args, opts)
	var resp InvokeResponse
	err := t.http.PostJSON(
		ctx,
		"/v1/invoke",
		&resp,
		WithJSON(body),
		WithNotFoundCode(ErrToolNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &resp, nil
}

// DryRun returns what an Invoke would do without actually calling the
// underlying tool. Useful for cost/latency budgeting.
func (t *ToolGateway) DryRun(ctx context.Context, toolID string, args map[string]any, opts InvokeOpts) (*DryRunResponse, error) {
	body := t.invokeBody(toolID, args, opts)
	var resp DryRunResponse
	err := t.http.PostJSON(
		ctx,
		"/v1/invoke/dry-run",
		&resp,
		WithJSON(body),
		WithNotFoundCode(ErrToolNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &resp, nil
}

// Register registers a new tool with the gateway.
func (t *ToolGateway) Register(ctx context.Context, reg ToolRegistration) (*Tool, error) {
	var tool Tool
	err := t.http.PostJSON(
		ctx,
		"/v1/tools/register",
		&tool,
		WithJSON(reg),
	)
	if err != nil {
		return nil, err
	}
	return &tool, nil
}

// List returns every registered tool.
func (t *ToolGateway) List(ctx context.Context) ([]Tool, error) {
	var resp struct {
		Tools []Tool `json:"tools"`
	}
	if err := t.http.GetJSON(ctx, "/v1/tools", &resp); err != nil {
		return nil, err
	}
	return resp.Tools, nil
}

// Get fetches a single registered tool by ID.
func (t *ToolGateway) Get(ctx context.Context, toolID string) (*Tool, error) {
	var tool Tool
	err := t.http.GetJSON(
		ctx,
		"/v1/tools/"+EncodePathSegment(toolID),
		&tool,
		WithNotFoundCode(ErrToolNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &tool, nil
}

// Deregister unregisters a tool from the gateway.
func (t *ToolGateway) Deregister(ctx context.Context, toolID string) error {
	return t.http.Delete(
		ctx,
		"/v1/tools/"+EncodePathSegment(toolID),
		WithNotFoundCode(ErrToolNotFound.Code),
	)
}

// Audit queries the gateway audit log.
func (t *ToolGateway) Audit(ctx context.Context, query AuditQuery) ([]AuditEvent, error) {
	q := url.Values{}
	if query.WorkspaceID != "" {
		q.Set("workspace_id", query.WorkspaceID)
	}
	if query.ToolID != "" {
		q.Set("tool_id", query.ToolID)
	}
	if query.Since != "" {
		q.Set("since", query.Since)
	}
	if query.Limit > 0 {
		q.Set("limit", strconv.Itoa(query.Limit))
	}
	var resp struct {
		Events []AuditEvent `json:"events"`
	}
	err := t.http.GetJSON(ctx, "/v1/audit", &resp, WithQuery(q))
	if err != nil {
		return nil, err
	}
	return resp.Events, nil
}

// invokeBody builds the InvokeRequest shared by Invoke and DryRun.
func (t *ToolGateway) invokeBody(toolID string, args map[string]any, opts InvokeOpts) InvokeRequest {
	body := InvokeRequest{
		ToolID:    toolID,
		Arguments: args,
	}
	if opts.WorkspaceID != "" {
		body.WorkspaceID = stringPtr(opts.WorkspaceID)
	}
	if opts.AgentID != "" {
		body.AgentID = stringPtr(opts.AgentID)
	}
	if opts.Cache != nil {
		body.Cache = opts.Cache
	}
	if opts.IdempotencyKey != "" {
		body.IdempotencyKey = stringPtr(opts.IdempotencyKey)
	}
	return body
}

// stringPtr is a tiny helper for the optional-string pattern. Kept
// here since multiple files use it; Go has no shorter way to spell a
// string pointer in field defaults.
func stringPtr(s string) *string { return &s }

// BoolPtr returns a pointer to b. Exported sugar for callers building
// an InvokeOpts who want to disable caching:
//
//	client.Tools.Invoke(ctx, "web.fetch", args, plinth.InvokeOpts{
//	    Cache: plinth.BoolPtr(false),
//	})
func BoolPtr(b bool) *bool { return &b }
