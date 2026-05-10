// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"net/url"
)

// WorkspaceClient is the entry point for every per-workspace
// operation. Construct via *Plinth.Workspace; it bundles the cached
// server record plus typed sub-clients for KV, Files, Snapshots,
// Branches, Channels, Workflows, and Locks.
//
// A single WorkspaceClient can be reused across goroutines as long as
// the underlying *http.Client also is (the standard library's default
// client is goroutine-safe). Switch to a branch-scoped view via
// WithBranch — the original is unchanged.
type WorkspaceClient struct {
	// Record is the snapshot of the server-side workspace at the time
	// this client was constructed. Re-fetch by calling
	// Plinth.GetWorkspace if the metadata may have drifted.
	Record Workspace

	// Sub-clients. Each holds a reference to the same shared *HTTPClient
	// and propagates the workspace ID + branch scope.
	KV        *KVProxy
	Files     *FilesProxy
	Snapshots *SnapshotsProxy
	Channels  *ChannelsProxy
	Workflows *WorkflowsProxy
	Locks     *LocksProxy

	http     *HTTPClient
	branchID string
}

// newWorkspaceClient wires up a fresh WorkspaceClient. Internal — use
// Plinth.Workspace / Plinth.GetWorkspace from application code.
func newWorkspaceClient(http *HTTPClient, ws Workspace, branchID string) *WorkspaceClient {
	w := &WorkspaceClient{Record: ws, http: http, branchID: branchID}
	w.KV = newKVProxy(w)
	w.Files = newFilesProxy(w)
	w.Snapshots = newSnapshotsProxy(w)
	w.Channels = newChannelsProxy(w)
	w.Workflows = newWorkflowsProxy(w)
	w.Locks = newLocksProxy(w)
	return w
}

// ID returns the stable workspace ID (e.g. "ws_01H…").
func (w *WorkspaceClient) ID() string { return w.Record.ID }

// Name returns the human-readable workspace name.
func (w *WorkspaceClient) Name() string { return w.Record.Name }

// BranchID returns the branch this client is scoped to, or "" for main.
func (w *WorkspaceClient) BranchID() string { return w.branchID }

// WithBranch returns a copy of this client scoped to branchID. All
// subsequent reads and writes via the returned client automatically
// append "?branch=<id>" to the underlying request.
func (w *WorkspaceClient) WithBranch(branchID string) *WorkspaceClient {
	return newWorkspaceClient(w.http, w.Record, branchID)
}

// branchQuery returns "?branch=<id>" or empty when not scoped. Helpers
// in the proxy files compose this with their own params.
func (w *WorkspaceClient) branchQuery() url.Values {
	q := url.Values{}
	if w.branchID != "" {
		q.Set("branch", w.branchID)
	}
	return q
}

// Snapshot is a top-level shortcut for ws.Snapshots.Create — matches
// the Python SDK's `ws.snapshot(...)` ergonomics.
func (w *WorkspaceClient) Snapshot(ctx context.Context, name, message string) (*Snapshot, error) {
	return w.Snapshots.Create(ctx, name, message)
}

// Diff diffs two snapshots — sugar for ws.Snapshots.Diff.
func (w *WorkspaceClient) Diff(ctx context.Context, snapshotA, snapshotB string) (*DiffResult, error) {
	return w.Snapshots.Diff(ctx, snapshotA, snapshotB)
}

// Branch creates a new branch starting from fromSnapshot.
func (w *WorkspaceClient) Branch(ctx context.Context, name, fromSnapshot string) (*Branch, error) {
	var b Branch
	err := w.http.PostJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(w.ID())+"/branches",
		&b,
		WithJSON(map[string]any{"name": name, "from_snapshot": fromSnapshot}),
		WithNotFoundCode(ErrSnapshotNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &b, nil
}

// Branches lists every branch on the workspace.
func (w *WorkspaceClient) Branches(ctx context.Context) ([]Branch, error) {
	var resp struct {
		Branches []Branch `json:"branches"`
	}
	err := w.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(w.ID())+"/branches",
		&resp,
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Branches, nil
}

// Merge merges branchID back into the workspace's main timeline.
func (w *WorkspaceClient) Merge(ctx context.Context, branchID string) (*MergeResult, error) {
	var mr MergeResult
	err := w.http.PostJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(w.ID())+"/branches/"+EncodePathSegment(branchID)+"/merge",
		&mr,
		WithNotFoundCode(ErrBranchNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &mr, nil
}

// DeleteBranch deletes a branch without merging.
func (w *WorkspaceClient) DeleteBranch(ctx context.Context, branchID string) error {
	return w.http.Delete(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(w.ID())+"/branches/"+EncodePathSegment(branchID),
		WithNotFoundCode(ErrBranchNotFound.Code),
	)
}
