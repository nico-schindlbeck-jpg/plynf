// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/plinth/sdk-go/plinth"
)

// TestWorkersRegister covers the worker-registration round-trip.
// Verifies the SDK fills hostname / pid defaults when the caller
// passes nil pointers.
func TestWorkersRegister(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("POST", "/v1/workers/register", 201, map[string]any{
		"id":                "worker_1",
		"hostname":          "test-host",
		"pid":               123,
		"started_at":        now,
		"last_heartbeat_at": now,
		"status":            "active",
	})
	c := newTestClient(t, ms)

	w, err := c.Workers.Register(context.Background(), plinth.WorkerRegistration{})
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if w.ID != "worker_1" {
		t.Errorf("worker.ID = %q, want worker_1", w.ID)
	}
	if w.Status != plinth.WorkerStatusActive {
		t.Errorf("status = %q, want active", w.Status)
	}

	// Body should carry non-nil hostname + pid even when the caller
	// passed an empty WorkerRegistration.
	var body map[string]any
	ms.LastRequest(t).jsonBody(t, &body)
	if _, ok := body["hostname"]; !ok {
		t.Error("body missing hostname (default should be set)")
	}
	if _, ok := body["pid"]; !ok {
		t.Error("body missing pid (default should be set)")
	}
}

// TestWorkersHeartbeatAndDrain covers the heartbeat + drain endpoints.
func TestWorkersHeartbeatAndDrain(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("POST", "/v1/workers/worker_1/heartbeat", 200, map[string]any{
		"id":                "worker_1",
		"started_at":        now,
		"last_heartbeat_at": now,
		"status":            "active",
	})
	ms.JSON("POST", "/v1/workers/worker_1/drain", 200, map[string]any{
		"id":                "worker_1",
		"started_at":        now,
		"last_heartbeat_at": now,
		"status":            "draining",
	})

	c := newTestClient(t, ms)
	if _, err := c.Workers.Heartbeat(context.Background(), "worker_1"); err != nil {
		t.Fatalf("Heartbeat: %v", err)
	}
	w, err := c.Workers.Drain(context.Background(), "worker_1")
	if err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if w.Status != plinth.WorkerStatusDraining {
		t.Errorf("status = %q, want draining", w.Status)
	}
}

// TestWorkersList verifies the listing endpoint and that the optional
// status filter is encoded as ?status=…
func TestWorkersList(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/workers", 200, map[string]any{
		"workers": []any{
			map[string]any{
				"id":                "worker_1",
				"started_at":        now,
				"last_heartbeat_at": now,
				"status":            "active",
			},
		},
	})
	c := newTestClient(t, ms)
	all, err := c.Workers.List(context.Background(), "active")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(all) != 1 || all[0].ID != "worker_1" {
		t.Errorf("List = %v, want one worker", all)
	}
	if got := ms.LastRequest(t).Query; got != "status=active" {
		t.Errorf("query = %q, want status=active", got)
	}
}

// TestWorkersHeartbeatNotFound covers the typed-error path when the
// worker_id was never registered (or has been swept).
func TestWorkersHeartbeatNotFound(t *testing.T) {
	ms := NewMockServer(t)
	ms.Error("POST", "/v1/workers/missing/heartbeat",
		404, "WORKER_NOT_FOUND", "no such worker")
	c := newTestClient(t, ms)
	_, err := c.Workers.Heartbeat(context.Background(), "missing")
	if !errors.Is(err, plinth.ErrWorkerNotFound) {
		t.Errorf("err = %v, want ErrWorkerNotFound", err)
	}
}
