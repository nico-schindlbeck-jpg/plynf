// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"net/url"
	"os"
)

// WorkersClient is the workspace service's worker registration
// surface (v0.5). Used by the workflow worker harness — application
// code rarely registers a worker directly.
type WorkersClient struct {
	http *HTTPClient
}

// NewWorkersClient constructs a client bound to a workspace HTTPClient.
// Exported so tests can wire one up directly.
func NewWorkersClient(workspaceHTTP *HTTPClient) *WorkersClient {
	return &WorkersClient{http: workspaceHTTP}
}

// Register registers a new worker process. Hostname and PID default
// to the current process's values when reg.Hostname / reg.PID are nil.
func (c *WorkersClient) Register(ctx context.Context, reg WorkerRegistration) (*Worker, error) {
	body := map[string]any{}
	if reg.Hostname != nil {
		body["hostname"] = *reg.Hostname
	} else {
		body["hostname"] = safeHostname()
	}
	if reg.PID != nil {
		body["pid"] = *reg.PID
	} else {
		body["pid"] = os.Getpid()
	}
	var w Worker
	if err := c.http.PostJSON(ctx, "/v1/workers/register", &w, WithJSON(body)); err != nil {
		return nil, err
	}
	return &w, nil
}

// Heartbeat bumps last_heartbeat_at for workerID.
func (c *WorkersClient) Heartbeat(ctx context.Context, workerID string) (*Worker, error) {
	var w Worker
	err := c.http.PostJSON(
		ctx,
		"/v1/workers/"+EncodePathSegment(workerID)+"/heartbeat",
		&w,
		WithNotFoundCode(ErrWorkerNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &w, nil
}

// Drain marks workerID as draining (graceful-shutdown signal).
func (c *WorkersClient) Drain(ctx context.Context, workerID string) (*Worker, error) {
	var w Worker
	err := c.http.PostJSON(
		ctx,
		"/v1/workers/"+EncodePathSegment(workerID)+"/drain",
		&w,
		WithNotFoundCode(ErrWorkerNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &w, nil
}

// List returns registered workers, optionally filtered by status.
// Pass "" for status to list every worker.
func (c *WorkersClient) List(ctx context.Context, status string) ([]Worker, error) {
	q := url.Values{}
	if status != "" {
		q.Set("status", status)
	}
	var resp struct {
		Workers []Worker `json:"workers"`
	}
	if err := c.http.GetJSON(ctx, "/v1/workers", &resp, WithQuery(q)); err != nil {
		return nil, err
	}
	return resp.Workers, nil
}

// safeHostname returns os.Hostname() or "" on failure. Mirrors the
// TS SDK's safeHostname helper — the workspace service tolerates an
// empty string so we degrade rather than fail boot.
func safeHostname() string {
	h, err := os.Hostname()
	if err != nil {
		return ""
	}
	return h
}
