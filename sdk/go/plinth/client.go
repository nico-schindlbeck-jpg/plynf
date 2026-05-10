// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

// Package plinth is the Go SDK for Plinth — workspaces, tools,
// identity, workers, and workflows.
//
// Construct one *Plinth per process; it owns one HTTPClient per
// backing service (workspace, gateway, identity) and exposes typed
// sub-clients reachable via the public fields:
//
//	client, err := plinth.New(plinth.Config{
//	    WorkspaceURL: "http://localhost:7421",
//	    GatewayURL:   "http://localhost:7422",
//	    IdentityURL:  "http://localhost:7425", // optional
//	    APIKey:       "local-dev",
//	})
//	if err != nil { /* ... */ }
//
//	ws, err := client.Workspace(ctx, "research-task-1")
//	err = ws.KV.Set(ctx, "topic", "renewable energy")
//
// Mirrors the Python SDK (sdk/python/src/plinth/client.py) and the
// TypeScript SDK (sdk/typescript/src/client.ts).
package plinth

import (
	"context"
	"net/http"
	"time"
)

// Default endpoints used when Config leaves the corresponding URL
// blank. Match the docker-compose defaults in CONTRACTS.md.
const (
	DefaultWorkspaceURL = "http://localhost:7421"
	DefaultGatewayURL   = "http://localhost:7422"
	DefaultIdentityURL  = "http://localhost:7425"
	DefaultUserAgent    = "plinth-sdk-go/0.1.0"
	DefaultTimeout      = 30 * time.Second
)

// Config bundles the constructor arguments for New.
//
// At minimum WorkspaceURL, GatewayURL, and APIKey must be supplied.
// IdentityURL is opt-in; ops/test code that mints capability tokens
// should set it. HTTPClient and UserAgent default to safe values.
type Config struct {
	// WorkspaceURL is the base URL of the workspace service.
	WorkspaceURL string

	// GatewayURL is the base URL of the gateway service.
	GatewayURL string

	// IdentityURL is the base URL of the identity service. Optional —
	// when blank, *Plinth.Identity returns ErrIdentityNotConfigured on
	// every call.
	IdentityURL string

	// APIKey is the bearer token sent on every request. In local dev,
	// any non-empty string works.
	APIKey string

	// HTTPClient is the *http.Client used for every request. When nil,
	// a sensible default with a 30s timeout is constructed.
	HTTPClient *http.Client

	// UserAgent is sent in the User-Agent header. Defaults to
	// "plinth-sdk-go/0.1.0".
	UserAgent string
}

// Plinth is the top-level SDK facade. Field types are exposed so users
// can hold sub-clients directly (e.g. pass *plinth.ToolGateway to a
// helper) without the facade getting in the way.
//
// Construct with New; never zero-initialise.
type Plinth struct {
	// Tools is the gateway's tool surface (invoke, register, audit).
	Tools *ToolGateway

	// Workers is the workspace's worker registration surface (v0.5).
	// Used by the workflow worker harness; application code rarely
	// touches it directly.
	Workers *WorkersClient

	// Identity is the identity service client (token issue/verify/
	// revoke, signing-key rotation, tenant quotas). nil when
	// Config.IdentityURL was blank — call client.IdentityClient() for
	// a guarded accessor that returns ErrIdentityNotConfigured.
	Identity *IdentityClient

	// HTTP clients exposed for the rare advanced use case of poking
	// at endpoints the SDK doesn't yet wrap. Most callers should
	// stick to the typed sub-clients above.
	WorkspaceHTTP *HTTPClient
	GatewayHTTP   *HTTPClient
	IdentityHTTP  *HTTPClient
}

// New validates cfg and returns a fully-wired *Plinth. Returns
// ErrInvalidConfig with a descriptive Message when WorkspaceURL,
// GatewayURL, or APIKey is missing.
func New(cfg Config) (*Plinth, error) {
	if cfg.APIKey == "" {
		return nil, &PlinthError{
			Code:    ErrInvalidConfig.Code,
			Message: "Config.APIKey is required (in local dev, any non-empty string)",
		}
	}
	if cfg.WorkspaceURL == "" {
		cfg.WorkspaceURL = DefaultWorkspaceURL
	}
	if cfg.GatewayURL == "" {
		cfg.GatewayURL = DefaultGatewayURL
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: DefaultTimeout}
	}
	if cfg.UserAgent == "" {
		cfg.UserAgent = DefaultUserAgent
	}

	p := &Plinth{
		WorkspaceHTTP: NewHTTPClient(cfg.HTTPClient, cfg.WorkspaceURL, cfg.APIKey, cfg.UserAgent),
		GatewayHTTP:   NewHTTPClient(cfg.HTTPClient, cfg.GatewayURL, cfg.APIKey, cfg.UserAgent),
	}
	p.Tools = NewToolGateway(p.GatewayHTTP)
	p.Workers = NewWorkersClient(p.WorkspaceHTTP)

	if cfg.IdentityURL != "" {
		p.IdentityHTTP = NewHTTPClient(cfg.HTTPClient, cfg.IdentityURL, cfg.APIKey, cfg.UserAgent)
		p.Identity = NewIdentityClient(p.IdentityHTTP)
	}
	return p, nil
}

// IdentityClient returns the configured identity client, or
// ErrIdentityNotConfigured if no IdentityURL was supplied to New.
//
// Prefer this over a nil-check on p.Identity when the caller wants a
// clear error path (e.g. CLI subcommands that always need identity).
func (p *Plinth) IdentityClient() (*IdentityClient, error) {
	if p.Identity == nil {
		return nil, &PlinthError{
			Code:    ErrIdentityNotConfigured.Code,
			Message: "identity service not configured: pass IdentityURL to plinth.New",
		}
	}
	return p.Identity, nil
}

// Workspace returns a get-or-create handle for the workspace named
// `name`. If multiple workspaces share the name, the most recently
// updated one wins (deterministic tiebreak so tests stay stable).
//
// Mirrors Python's client.workspace(name) and TS's client.workspace(name).
func (p *Plinth) Workspace(ctx context.Context, name string) (*WorkspaceClient, error) {
	existing, err := p.findWorkspaceByName(ctx, name)
	if err != nil {
		return nil, err
	}
	if existing != nil {
		return newWorkspaceClient(p.WorkspaceHTTP, *existing, ""), nil
	}
	return p.createWorkspace(ctx, name)
}

// GetWorkspace fetches a workspace by stable ID — useful when the
// caller already knows the ID (passed from another service, persisted
// in env, etc.) and wants to avoid the get-or-create list scan.
func (p *Plinth) GetWorkspace(ctx context.Context, workspaceID string) (*WorkspaceClient, error) {
	var ws Workspace
	err := p.WorkspaceHTTP.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(workspaceID),
		&ws,
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return newWorkspaceClient(p.WorkspaceHTTP, ws, ""), nil
}

// ListWorkspaces returns every workspace visible to the API key.
func (p *Plinth) ListWorkspaces(ctx context.Context) ([]Workspace, error) {
	var resp struct {
		Workspaces []Workspace `json:"workspaces"`
	}
	if err := p.WorkspaceHTTP.GetJSON(ctx, "/v1/workspaces", &resp); err != nil {
		return nil, err
	}
	return resp.Workspaces, nil
}

// DeleteWorkspace permanently deletes a workspace and all of its
// versioned data.
func (p *Plinth) DeleteWorkspace(ctx context.Context, workspaceID string) error {
	return p.WorkspaceHTTP.Delete(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(workspaceID),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
}

// CountTokens returns an offline approximation of the cl100k token
// count for text. See tokens.go for the full algorithm.
//
// Method on *Plinth (rather than a free function) so the surface
// matches the Python SDK's client.count_tokens.
func (p *Plinth) CountTokens(text string) int {
	return CountTokens(text)
}

// EstimateCost returns a USD estimate for a Sonnet-class request given
// its input/output token counts. Pass 0 for completionTokens to get
// the prompt-only cost.
func (p *Plinth) EstimateCost(promptTokens, completionTokens int) float64 {
	return EstimateCost(promptTokens, completionTokens)
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

func (p *Plinth) findWorkspaceByName(ctx context.Context, name string) (*Workspace, error) {
	all, err := p.ListWorkspaces(ctx)
	if err != nil {
		return nil, err
	}
	var match *Workspace
	for i := range all {
		if all[i].Name != name {
			continue
		}
		// Deterministic tiebreak: prefer the most recently updated.
		if match == nil || all[i].UpdatedAt.After(match.UpdatedAt) {
			match = &all[i]
		}
	}
	return match, nil
}

func (p *Plinth) createWorkspace(ctx context.Context, name string) (*WorkspaceClient, error) {
	var ws Workspace
	err := p.WorkspaceHTTP.PostJSON(
		ctx,
		"/v1/workspaces",
		&ws,
		WithJSON(map[string]any{"name": name}),
	)
	if err != nil {
		return nil, err
	}
	return newWorkspaceClient(p.WorkspaceHTTP, ws, ""), nil
}
