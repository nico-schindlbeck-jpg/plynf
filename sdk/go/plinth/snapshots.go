// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
)

// SnapshotsProxy exposes snapshot operations on a workspace. Reachable
// via *WorkspaceClient.Snapshots; the WorkspaceClient itself also
// proxies the Create/Diff calls for ergonomics
// (ws.Snapshot(...) / ws.Diff(...)).
type SnapshotsProxy struct {
	ws *WorkspaceClient
}

func newSnapshotsProxy(ws *WorkspaceClient) *SnapshotsProxy { return &SnapshotsProxy{ws: ws} }

// Create captures the current latest version of every key/file as a
// new immutable snapshot. message is optional — pass "" to skip.
func (s *SnapshotsProxy) Create(ctx context.Context, name, message string) (*Snapshot, error) {
	body := map[string]any{"name": name}
	if message != "" {
		body["message"] = message
	}
	var snap Snapshot
	err := s.ws.http.PostJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(s.ws.ID())+"/snapshots",
		&snap,
		WithJSON(body),
		WithQuery(s.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &snap, nil
}

// List returns every snapshot in the workspace (newest last).
func (s *SnapshotsProxy) List(ctx context.Context) ([]Snapshot, error) {
	var resp struct {
		Snapshots []Snapshot `json:"snapshots"`
	}
	err := s.ws.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(s.ws.ID())+"/snapshots",
		&resp,
		WithQuery(s.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Snapshots, nil
}

// Get fetches a single snapshot by ID.
func (s *SnapshotsProxy) Get(ctx context.Context, snapshotID string) (*Snapshot, error) {
	var snap Snapshot
	err := s.ws.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(s.ws.ID())+"/snapshots/"+EncodePathSegment(snapshotID),
		&snap,
		WithNotFoundCode(ErrSnapshotNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &snap, nil
}

// Diff returns the diff between snapshots a and b.
func (s *SnapshotsProxy) Diff(ctx context.Context, a, b string) (*DiffResult, error) {
	var diff DiffResult
	q := s.ws.branchQuery()
	q.Set("against", b)
	err := s.ws.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(s.ws.ID())+"/snapshots/"+EncodePathSegment(a)+"/diff",
		&diff,
		WithQuery(q),
		WithNotFoundCode(ErrSnapshotNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &diff, nil
}
