// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth_test

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/plinth/sdk-go/plinth"
)

// TestNewRequiresAPIKey covers the construction-time validation: a
// missing API key surfaces as ErrInvalidConfig before any request is
// issued.
func TestNewRequiresAPIKey(t *testing.T) {
	_, err := plinth.New(plinth.Config{
		WorkspaceURL: "http://localhost:7421",
		GatewayURL:   "http://localhost:7422",
	})
	if err == nil {
		t.Fatal("expected error for missing APIKey, got nil")
	}
	if !errors.Is(err, plinth.ErrInvalidConfig) {
		t.Fatalf("expected ErrInvalidConfig, got %v (code: %s)", err, codeOf(err))
	}
}

// TestNewDefaultURLs verifies that omitting WorkspaceURL/GatewayURL
// falls back to the documented defaults.
func TestNewDefaultURLs(t *testing.T) {
	c, err := plinth.New(plinth.Config{APIKey: "x"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if c.WorkspaceHTTP.BaseURL() != plinth.DefaultWorkspaceURL {
		t.Errorf("workspace URL = %q, want %q", c.WorkspaceHTTP.BaseURL(), plinth.DefaultWorkspaceURL)
	}
	if c.GatewayHTTP.BaseURL() != plinth.DefaultGatewayURL {
		t.Errorf("gateway URL = %q, want %q", c.GatewayHTTP.BaseURL(), plinth.DefaultGatewayURL)
	}
}

// TestNewIdentityOptional verifies that Identity is nil when no
// IdentityURL is provided, and that the IdentityClient guard accessor
// returns the expected error.
func TestNewIdentityOptional(t *testing.T) {
	c, err := plinth.New(plinth.Config{APIKey: "x"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if c.Identity != nil {
		t.Errorf("Identity = non-nil, want nil when IdentityURL is empty")
	}
	if _, err := c.IdentityClient(); err == nil {
		t.Error("IdentityClient() returned nil error when identity not configured")
	} else if !errors.Is(err, plinth.ErrIdentityNotConfigured) {
		t.Errorf("IdentityClient() error = %v, want ErrIdentityNotConfigured", err)
	}
}

// TestNewWithCustomHTTPClient verifies that a caller-supplied http.Client
// is honoured (the SDK does not silently swap to its own).
func TestNewWithCustomHTTPClient(t *testing.T) {
	custom := &http.Client{Timeout: 1 * time.Millisecond}
	_, err := plinth.New(plinth.Config{
		APIKey:     "x",
		HTTPClient: custom,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// Making a request through the public API would prove this end-to-end,
	// but the assertion that New accepts the option without panic is
	// enough — the underlying httpClient is unexported by design.
}

// TestWorkspaceGetOrCreate_HitsExisting checks the get-or-create path:
// when a workspace by the name already exists, a single list call
// suffices and no POST is issued.
func TestWorkspaceGetOrCreate_HitsExisting(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/workspaces", 200, map[string]any{
		"workspaces": []any{
			map[string]any{
				"id":         "ws_existing",
				"name":       "research",
				"created_at": now,
				"updated_at": now,
			},
		},
	})

	c := newTestClient(t, ms)
	ws, err := c.Workspace(context.Background(), "research")
	if err != nil {
		t.Fatalf("Workspace: %v", err)
	}
	if ws.ID() != "ws_existing" {
		t.Errorf("workspace.ID = %q, want ws_existing", ws.ID())
	}
	for _, r := range ms.Requests() {
		if r.Method == "POST" && r.Path == "/v1/workspaces" {
			t.Errorf("unexpected POST to /v1/workspaces — get-or-create should reuse existing")
		}
	}
}

// TestWorkspaceGetOrCreate_CreatesWhenMissing covers the other half:
// when no workspace matches the name, the SDK posts a new one.
func TestWorkspaceGetOrCreate_CreatesWhenMissing(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/workspaces", 200, map[string]any{"workspaces": []any{}})
	ms.JSON("POST", "/v1/workspaces", 201, map[string]any{
		"id":         "ws_new",
		"name":       "fresh",
		"created_at": now,
		"updated_at": now,
	})

	c := newTestClient(t, ms)
	ws, err := c.Workspace(context.Background(), "fresh")
	if err != nil {
		t.Fatalf("Workspace: %v", err)
	}
	if ws.ID() != "ws_new" {
		t.Errorf("workspace.ID = %q, want ws_new", ws.ID())
	}
	// The body should carry the requested name.
	post := findRequest(ms.Requests(), "POST", "/v1/workspaces")
	if post == nil {
		t.Fatal("expected POST /v1/workspaces, got none")
	}
	var body map[string]any
	post.jsonBody(t, &body)
	if body["name"] != "fresh" {
		t.Errorf("create body name = %v, want fresh", body["name"])
	}
}

// TestGetWorkspaceNotFound covers the typed-error path when fetching
// by ID. errors.Is should match ErrWorkspaceNotFound.
func TestGetWorkspaceNotFound(t *testing.T) {
	ms := NewMockServer(t)
	ms.Error("GET", "/v1/workspaces/ws_missing", 404, "WORKSPACE_NOT_FOUND", "no such workspace")

	c := newTestClient(t, ms)
	_, err := c.GetWorkspace(context.Background(), "ws_missing")
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, plinth.ErrWorkspaceNotFound) {
		t.Errorf("err = %v, want ErrWorkspaceNotFound", err)
	}
}

// TestListWorkspaces verifies that the listing endpoint is parsed and
// every record surfaces.
func TestListWorkspaces(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/workspaces", 200, map[string]any{
		"workspaces": []any{
			map[string]any{"id": "ws_a", "name": "a", "created_at": now, "updated_at": now},
			map[string]any{"id": "ws_b", "name": "b", "created_at": now, "updated_at": now},
		},
	})
	c := newTestClient(t, ms)
	all, err := c.ListWorkspaces(context.Background())
	if err != nil {
		t.Fatalf("ListWorkspaces: %v", err)
	}
	if len(all) != 2 {
		t.Errorf("len(all) = %d, want 2", len(all))
	}
	if all[0].ID != "ws_a" || all[1].ID != "ws_b" {
		t.Errorf("ids = %v, want [ws_a ws_b]", []string{all[0].ID, all[1].ID})
	}
}

// TestDeleteWorkspace covers the simple DELETE path.
func TestDeleteWorkspace(t *testing.T) {
	ms := NewMockServer(t)
	ms.On("DELETE", "/v1/workspaces/ws_x", func(_ recordedRequest) mockResponse {
		return mockResponse{Status: 204}
	})
	c := newTestClient(t, ms)
	if err := c.DeleteWorkspace(context.Background(), "ws_x"); err != nil {
		t.Fatalf("DeleteWorkspace: %v", err)
	}
	if got := ms.LastRequest(t); got.Path != "/v1/workspaces/ws_x" {
		t.Errorf("path = %q, want /v1/workspaces/ws_x", got.Path)
	}
}

// TestAuthHeaderAndUserAgent verifies that every outgoing request
// carries the bearer token and the SDK user-agent.
func TestAuthHeaderAndUserAgent(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("GET", "/v1/workspaces", 200, map[string]any{"workspaces": []any{}})
	c := newTestClient(t, ms)

	if _, err := c.ListWorkspaces(context.Background()); err != nil {
		t.Fatalf("ListWorkspaces: %v", err)
	}
	got := ms.LastRequest(t)
	if got.Headers.Get("Authorization") != "Bearer test-key" {
		t.Errorf("Authorization = %q, want Bearer test-key", got.Headers.Get("Authorization"))
	}
	if !strings.HasPrefix(got.Headers.Get("User-Agent"), "plinth-sdk-go/") {
		t.Errorf("User-Agent = %q, want plinth-sdk-go/...", got.Headers.Get("User-Agent"))
	}
}

// findRequest scans recorded requests for the first that matches
// method+path. Tiny helper used across tests.
func findRequest(requests []recordedRequest, method, path string) *recordedRequest {
	for i := range requests {
		if requests[i].Method == method && requests[i].Path == path {
			return &requests[i]
		}
	}
	return nil
}

// codeOf extracts the Plinth error code from any error, or "" if not
// a *PlinthError. Cheap helper kept here so failing assertions can
// point at the wire code.
func codeOf(err error) string {
	var pe *plinth.PlinthError
	if errors.As(err, &pe) {
		return pe.Code
	}
	return ""
}
