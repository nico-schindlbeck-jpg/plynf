// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth_test

import (
	"context"
	"errors"
	"testing"

	"github.com/plinth/sdk-go/plinth"
)

// TestErrorEnvelopeParsing verifies the SDK decodes
// `{"error":{"code","message","details"}}` into PlinthError fields.
func TestErrorEnvelopeParsing(t *testing.T) {
	ms := NewMockServer(t)
	ms.ErrorWithDetails("GET", "/v1/workspaces/ws_x", 404,
		"WORKSPACE_NOT_FOUND",
		"Workspace ws_x does not exist",
		map[string]any{"requested_id": "ws_x"},
	)
	c := newTestClient(t, ms)
	_, err := c.GetWorkspace(context.Background(), "ws_x")

	var pe *plinth.PlinthError
	if !errors.As(err, &pe) {
		t.Fatalf("errors.As failed: %v", err)
	}
	if pe.Code != "WORKSPACE_NOT_FOUND" {
		t.Errorf("Code = %q, want WORKSPACE_NOT_FOUND", pe.Code)
	}
	if pe.Message != "Workspace ws_x does not exist" {
		t.Errorf("Message = %q, want \"Workspace ws_x does not exist\"", pe.Message)
	}
	if pe.StatusCode != 404 {
		t.Errorf("StatusCode = %d, want 404", pe.StatusCode)
	}
	if pe.Details["requested_id"] != "ws_x" {
		t.Errorf("Details.requested_id = %v, want ws_x", pe.Details["requested_id"])
	}
}

// TestErrorIsDispatchTable verifies that every code-to-sentinel
// mapping in the SDK works with errors.Is.
func TestErrorIsDispatchTable(t *testing.T) {
	cases := []struct {
		code     string
		sentinel *plinth.PlinthError
	}{
		{"WORKSPACE_NOT_FOUND", plinth.ErrWorkspaceNotFound},
		{"KEY_NOT_FOUND", plinth.ErrKeyNotFound},
		{"FILE_NOT_FOUND", plinth.ErrFileNotFound},
		{"SNAPSHOT_NOT_FOUND", plinth.ErrSnapshotNotFound},
		{"BRANCH_NOT_FOUND", plinth.ErrBranchNotFound},
		{"TOOL_NOT_FOUND", plinth.ErrToolNotFound},
		{"CHANNEL_NOT_FOUND", plinth.ErrChannelNotFound},
		{"WORKFLOW_NOT_FOUND", plinth.ErrWorkflowNotFound},
		{"WORKFLOW_STEP_NOT_FOUND", plinth.ErrWorkflowStepNotFound},
		{"INVALID_WORKFLOW_STEP", plinth.ErrInvalidWorkflowStep},
		{"UNAUTHORIZED", plinth.ErrUnauthorized},
		{"INVALID_TOKEN", plinth.ErrInvalidToken},
		{"TOKEN_EXPIRED", plinth.ErrTokenExpired},
		{"TOKEN_REVOKED", plinth.ErrTokenRevoked},
		{"RATE_LIMITED", plinth.ErrRateLimited},
		{"COST_CAP_EXCEEDED", plinth.ErrCostCapExceeded},
		{"LEASE_CONFLICT", plinth.ErrLeaseConflict},
		{"LEASE_NOT_HELD", plinth.ErrLeaseNotHeld},
		{"WORKER_NOT_FOUND", plinth.ErrWorkerNotFound},
	}
	for _, tc := range cases {
		t.Run(tc.code, func(t *testing.T) {
			ms := NewMockServer(t)
			ms.Error("GET", "/v1/probe", 400, tc.code, tc.code)
			c := newTestClient(t, ms)
			// Call something — the URL doesn't matter, the route is hit.
			_, err := c.WorkspaceHTTP.Get(context.Background(), "/v1/probe")
			if !errors.Is(err, tc.sentinel) {
				t.Errorf("errors.Is(err, %s) = false (err = %v)", tc.code, err)
			}
		})
	}
}

// TestErrorFallbackToStatusCodeMap verifies that, when the server
// omits the code, the SDK still picks the right typed error from the
// status-code fallback map.
func TestErrorFallbackToStatusCodeMap(t *testing.T) {
	ms := NewMockServer(t)
	ms.On("GET", "/v1/workspaces", func(_ recordedRequest) mockResponse {
		// No "error" envelope at all — just a 401 with some body.
		return mockResponse{Status: 401, JSONBody: map[string]any{"detail": "go away"}}
	})
	c := newTestClient(t, ms)

	_, err := c.ListWorkspaces(context.Background())
	if !errors.Is(err, plinth.ErrUnauthorized) {
		t.Errorf("err = %v, want ErrUnauthorized via fallback", err)
	}
}

// TestErrorIsCode covers the IsCode helper.
func TestErrorIsCode(t *testing.T) {
	ms := NewMockServer(t)
	ms.Error("GET", "/v1/workspaces", 404, "WORKSPACE_NOT_FOUND", "no")
	c := newTestClient(t, ms)
	_, err := c.ListWorkspaces(context.Background())
	if !plinth.IsCode(err, "WORKSPACE_NOT_FOUND") {
		t.Errorf("IsCode(WORKSPACE_NOT_FOUND) = false")
	}
	if plinth.IsCode(err, "TOOL_NOT_FOUND") {
		t.Errorf("IsCode(TOOL_NOT_FOUND) = true, want false")
	}
}

// TestErrorBodyIsCaptured verifies the raw response body is preserved
// on PlinthError.Body so callers can debug malformed envelopes.
func TestErrorBodyIsCaptured(t *testing.T) {
	ms := NewMockServer(t)
	ms.On("GET", "/v1/workspaces", func(_ recordedRequest) mockResponse {
		return mockResponse{
			Status:  500,
			RawBody: []byte("plain text panic\n"),
			ContentType: "text/plain",
		}
	})
	c := newTestClient(t, ms)
	_, err := c.ListWorkspaces(context.Background())
	var pe *plinth.PlinthError
	if !errors.As(err, &pe) {
		t.Fatalf("errors.As failed: %v", err)
	}
	if string(pe.Body) != "plain text panic\n" {
		t.Errorf("Body = %q, want \"plain text panic\\n\"", string(pe.Body))
	}
	if pe.StatusCode != 500 {
		t.Errorf("StatusCode = %d, want 500", pe.StatusCode)
	}
}

// TestErrorConnectionWrapped verifies network failures surface as
// ErrConnectionError so callers can distinguish them from
// service-side errors.
func TestErrorConnectionWrapped(t *testing.T) {
	c, err := plinth.New(plinth.Config{
		WorkspaceURL: "http://127.0.0.1:1", // closed port
		GatewayURL:   "http://127.0.0.1:1",
		APIKey:       "x",
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	_, callErr := c.ListWorkspaces(context.Background())
	if !errors.Is(callErr, plinth.ErrConnectionError) {
		t.Errorf("err = %v, want ErrConnectionError", callErr)
	}
}
