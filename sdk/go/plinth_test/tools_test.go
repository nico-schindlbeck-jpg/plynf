// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/plinth/sdk-go/plinth"
)

// TestToolsInvokeHappyPath covers the basic invoke flow: arguments
// flow through, the response decodes into InvokeResponse, and the
// returned cached/duration_ms fields are surfaced.
func TestToolsInvokeHappyPath(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("POST", "/v1/invoke", 200, map[string]any{
		"tool_id":   "web.fetch",
		"arguments": map[string]any{"url": "mock://test"},
		"result":    map[string]any{"content": "hello", "status": 200},
		"cached":    false,
		"duration_ms": 12,
		"audit_id":  "evt_abc",
		"cost_estimate_usd": 0.0,
	})
	c := newTestClient(t, ms)

	resp, err := c.Tools.Invoke(context.Background(), "web.fetch",
		map[string]any{"url": "mock://test"},
		plinth.InvokeOpts{},
	)
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	if resp.ToolID != "web.fetch" {
		t.Errorf("resp.ToolID = %q, want web.fetch", resp.ToolID)
	}
	if resp.AuditID != "evt_abc" {
		t.Errorf("resp.AuditID = %q, want evt_abc", resp.AuditID)
	}
	if resp.Cached {
		t.Errorf("resp.Cached = true, want false")
	}

	// Verify the request body shape.
	last := ms.LastRequest(t)
	var body map[string]any
	last.jsonBody(t, &body)
	if body["tool_id"] != "web.fetch" {
		t.Errorf("body.tool_id = %v, want web.fetch", body["tool_id"])
	}
}

// TestToolsInvokeCachedFlag verifies the response signals a cache hit.
func TestToolsInvokeCachedFlag(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("POST", "/v1/invoke", 200, map[string]any{
		"tool_id":   "web.fetch",
		"arguments": map[string]any{"url": "mock://test"},
		"result":    map[string]any{"content": "cached"},
		"cached":    true,
		"duration_ms": 0,
		"audit_id":  "evt_def",
		"cost_estimate_usd": 0.0,
	})
	c := newTestClient(t, ms)
	resp, err := c.Tools.Invoke(context.Background(), "web.fetch",
		map[string]any{"url": "mock://test"},
		plinth.InvokeOpts{},
	)
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	if !resp.Cached {
		t.Errorf("resp.Cached = false, want true")
	}
}

// TestToolsInvokeNotFound covers the 404 path on POST /v1/invoke.
// errors.Is should match ErrToolNotFound.
func TestToolsInvokeNotFound(t *testing.T) {
	ms := NewMockServer(t)
	ms.Error("POST", "/v1/invoke", 404, "TOOL_NOT_FOUND", "no such tool")
	c := newTestClient(t, ms)

	_, err := c.Tools.Invoke(context.Background(), "no.such",
		map[string]any{}, plinth.InvokeOpts{})
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, plinth.ErrToolNotFound) {
		t.Errorf("err = %v, want ErrToolNotFound", err)
	}
}

// TestToolsInvokeCacheDisabledIsForwarded checks that InvokeOpts.Cache
// = BoolPtr(false) ends up in the request body so the gateway can act
// on it.
func TestToolsInvokeCacheDisabledIsForwarded(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("POST", "/v1/invoke", 200, map[string]any{
		"tool_id": "web.fetch", "arguments": map[string]any{}, "result": "ok",
		"cached": false, "duration_ms": 1, "audit_id": "x", "cost_estimate_usd": 0.0,
	})
	c := newTestClient(t, ms)

	if _, err := c.Tools.Invoke(context.Background(), "web.fetch",
		map[string]any{}, plinth.InvokeOpts{Cache: plinth.BoolPtr(false)},
	); err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	var body map[string]any
	ms.LastRequest(t).jsonBody(t, &body)
	if body["cache"] != false {
		t.Errorf("body.cache = %v, want false", body["cache"])
	}
}

// TestToolsRegisterAndList covers the /v1/tools/register +
// /v1/tools listing endpoints.
func TestToolsRegisterAndList(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("POST", "/v1/tools/register", 201, map[string]any{
		"tool_id":       "x.tool",
		"name":          "X Tool",
		"description":   "test",
		"transport":     "http",
		"endpoint":      "http://localhost:9000",
		"input_schema":  map[string]any{},
		"output_schema": map[string]any{},
		"created_at":    now,
		"updated_at":    now,
	})
	ms.JSON("GET", "/v1/tools", 200, map[string]any{
		"tools": []any{
			map[string]any{
				"tool_id":       "x.tool",
				"name":          "X Tool",
				"description":   "test",
				"transport":     "http",
				"endpoint":      "http://localhost:9000",
				"input_schema":  map[string]any{},
				"output_schema": map[string]any{},
				"created_at":    now,
				"updated_at":    now,
			},
		},
	})

	c := newTestClient(t, ms)
	tool, err := c.Tools.Register(context.Background(), plinth.ToolRegistration{
		ToolID:       "x.tool",
		Name:         "X Tool",
		Description:  "test",
		Transport:    plinth.ToolTransportHTTP,
		Endpoint:     "http://localhost:9000",
		InputSchema:  map[string]any{},
		OutputSchema: map[string]any{},
	})
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if tool.ToolID != "x.tool" {
		t.Errorf("tool.ToolID = %q, want x.tool", tool.ToolID)
	}

	all, err := c.Tools.List(context.Background())
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(all) != 1 || all[0].ToolID != "x.tool" {
		t.Errorf("List = %v, want one tool x.tool", all)
	}
}

// TestToolsAuditQueryEncodesParams verifies the audit query parameters
// are forwarded as ?workspace_id=&tool_id=&since=&limit=.
func TestToolsAuditQueryEncodesParams(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("GET", "/v1/audit", 200, map[string]any{"events": []any{}})
	c := newTestClient(t, ms)

	if _, err := c.Tools.Audit(context.Background(), plinth.AuditQuery{
		WorkspaceID: "ws_x",
		ToolID:      "web.fetch",
		Since:       "1h",
		Limit:       50,
	}); err != nil {
		t.Fatalf("Audit: %v", err)
	}
	got := ms.LastRequest(t)
	for _, k := range []string{"workspace_id=ws_x", "tool_id=web.fetch", "since=1h", "limit=50"} {
		if !strings.Contains(got.Query, k) {
			t.Errorf("query missing %q (got %q)", k, got.Query)
		}
	}
}

// TestToolsRateLimitedSurfacesRetryAfter verifies that 429 responses
// populate PlinthError.RetryAfter and LimitType.
func TestToolsRateLimitedSurfacesRetryAfter(t *testing.T) {
	ms := NewMockServer(t)
	ms.ErrorWithDetails("POST", "/v1/invoke", 429,
		"RATE_LIMITED", "too fast",
		map[string]any{
			"limit_type":          "rpm",
			"retry_after_seconds": 12.0,
			"current":             60,
			"limit":               60,
		},
	)
	c := newTestClient(t, ms)

	_, err := c.Tools.Invoke(context.Background(), "web.fetch",
		map[string]any{}, plinth.InvokeOpts{},
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, plinth.ErrRateLimited) {
		t.Errorf("err = %v, want ErrRateLimited", err)
	}
	var pe *plinth.PlinthError
	if !errors.As(err, &pe) {
		t.Fatalf("errors.As to *PlinthError failed: %v", err)
	}
	if pe.RetryAfter != 12.0 {
		t.Errorf("RetryAfter = %v, want 12.0", pe.RetryAfter)
	}
	if pe.LimitType != "rpm" {
		t.Errorf("LimitType = %q, want rpm", pe.LimitType)
	}
}

// TestToolsDryRun covers the /v1/invoke/dry-run endpoint.
func TestToolsDryRun(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("POST", "/v1/invoke/dry-run", 200, map[string]any{
		"tool_id":               "web.fetch",
		"arguments":             map[string]any{"url": "mock://"},
		"would_invoke":          true,
		"estimated_cost_usd":    0.001,
		"estimated_duration_ms": 50,
	})
	c := newTestClient(t, ms)
	resp, err := c.Tools.DryRun(context.Background(), "web.fetch",
		map[string]any{"url": "mock://"}, plinth.InvokeOpts{},
	)
	if err != nil {
		t.Fatalf("DryRun: %v", err)
	}
	if !resp.WouldInvoke {
		t.Errorf("WouldInvoke = false, want true")
	}
	if resp.EstimatedDurationMs != 50 {
		t.Errorf("EstimatedDurationMs = %d, want 50", resp.EstimatedDurationMs)
	}
}
