// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
)

// LocksProxy is the v0.6 generic distributed-lock surface for a
// workspace. Locks are independent of the workflow-step lease
// primitive — they coordinate any named object (KV key, file path,
// external resource handle) so two agents don't step on each other.
type LocksProxy struct {
	ws *WorkspaceClient
}

func newLocksProxy(ws *WorkspaceClient) *LocksProxy { return &LocksProxy{ws: ws} }

// LockAcquireOpts customises a single Acquire call.
type LockAcquireOpts struct {
	// TTLSeconds is how long the lock survives without a heartbeat.
	// Defaults to 60 when zero.
	TTLSeconds int
	// WaitMs, when positive, polls the server until the lock is free
	// or the budget elapses (then returns ErrLockConflict). Default 0
	// = fail-fast.
	WaitMs int
}

// Acquire takes a lock on name with the given holder identifier.
// Returns ErrLockConflict on contention (when WaitMs is 0 or expires).
func (l *LocksProxy) Acquire(ctx context.Context, name, holder string, opts LockAcquireOpts) (*Lock, error) {
	ttl := opts.TTLSeconds
	if ttl <= 0 {
		ttl = 60
	}
	body := map[string]any{
		"holder":      holder,
		"ttl_seconds": ttl,
		"wait_ms":     opts.WaitMs,
	}
	var lock Lock
	err := l.ws.http.PostJSON(
		ctx,
		l.path(name)+"/acquire",
		&lock,
		WithJSON(body),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &lock, nil
}

// Heartbeat extends the lock's TTL. Only the current holder may
// heartbeat. ttlSeconds <= 0 means "use the original TTL".
func (l *LocksProxy) Heartbeat(ctx context.Context, name, holder string, ttlSeconds int) (*Lock, error) {
	body := map[string]any{"holder": holder}
	if ttlSeconds > 0 {
		body["ttl_seconds"] = ttlSeconds
	}
	var lock Lock
	err := l.ws.http.PostJSON(
		ctx,
		l.path(name)+"/heartbeat",
		&lock,
		WithJSON(body),
		WithNotFoundCode(ErrLockNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &lock, nil
}

// Release releases a held lock. Idempotent — releasing a lock you
// don't hold (or one already swept) returns nil rather than error.
func (l *LocksProxy) Release(ctx context.Context, name, holder string) error {
	_, err := l.ws.http.Post(
		ctx,
		l.path(name)+"/release",
		WithJSON(map[string]any{"holder": holder}),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	return err
}

// List returns every lock currently persisted in the workspace.
func (l *LocksProxy) List(ctx context.Context) ([]Lock, error) {
	var resp struct {
		Locks []Lock `json:"locks"`
	}
	err := l.ws.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(l.ws.ID())+"/locks",
		&resp,
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Locks, nil
}

// Get fetches a single lock row.
func (l *LocksProxy) Get(ctx context.Context, name string) (*Lock, error) {
	var lock Lock
	err := l.ws.http.GetJSON(
		ctx,
		l.path(name),
		&lock,
		WithNotFoundCode(ErrLockNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &lock, nil
}

func (l *LocksProxy) path(name string) string {
	return "/v1/workspaces/" + EncodePathSegment(l.ws.ID()) + "/locks/" + EncodeLockName(name)
}
