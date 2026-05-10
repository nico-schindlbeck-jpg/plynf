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

// workspaceFixture wires a MockServer with the standard
// "GET /v1/workspaces returns one workspace" + "POST /v1/workspaces
// creates" routes so individual tests can call client.Workspace(...)
// without re-stating the get-or-create dance.
//
// Returns the *Plinth, the *WorkspaceClient, and the MockServer for
// further route registration.
func workspaceFixture(t *testing.T) (*plinth.Plinth, *plinth.WorkspaceClient, *MockServer) {
	t.Helper()
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/workspaces", 200, map[string]any{
		"workspaces": []any{
			map[string]any{
				"id":         "ws_test",
				"name":       "test-ws",
				"created_at": now,
				"updated_at": now,
			},
		},
	})

	c := newTestClient(t, ms)
	ws, err := c.Workspace(context.Background(), "test-ws")
	if err != nil {
		t.Fatalf("Workspace: %v", err)
	}
	return c, ws, ms
}

// TestKVSetAndGet covers the basic write/read round-trip plus version
// disclosure via GetWithVersion.
func TestKVSetAndGet(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)

	ms.JSON("PUT", "/v1/workspaces/ws_test/kv/topic", 200, map[string]any{
		"workspace_id": "ws_test",
		"key":          "topic",
		"value":        "renewable energy",
		"version":      1,
		"created_at":   now,
		"deleted":      false,
	})
	ms.JSON("GET", "/v1/workspaces/ws_test/kv/topic", 200, map[string]any{
		"workspace_id": "ws_test",
		"key":          "topic",
		"value":        "renewable energy",
		"version":      1,
		"created_at":   now,
		"deleted":      false,
	})

	if _, err := ws.KV.Set(context.Background(), "topic", "renewable energy"); err != nil {
		t.Fatalf("KV.Set: %v", err)
	}
	val, err := ws.KV.Get(context.Background(), "topic")
	if err != nil {
		t.Fatalf("KV.Get: %v", err)
	}
	if val != "renewable energy" {
		t.Errorf("KV.Get = %v, want \"renewable energy\"", val)
	}

	val2, version, err := ws.KV.GetWithVersion(context.Background(), "topic")
	if err != nil {
		t.Fatalf("KV.GetWithVersion: %v", err)
	}
	if val2 != "renewable energy" || version != 1 {
		t.Errorf("GetWithVersion = (%v, %d), want (renewable energy, 1)", val2, version)
	}
}

// TestKVHistory covers the /history endpoint.
func TestKVHistory(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/workspaces/ws_test/kv/topic/history", 200, map[string]any{
		"versions": []any{
			map[string]any{"workspace_id": "ws_test", "key": "topic", "value": "v1", "version": 1, "created_at": now, "deleted": false},
			map[string]any{"workspace_id": "ws_test", "key": "topic", "value": "v2", "version": 2, "created_at": now, "deleted": false},
		},
	})
	versions, err := ws.KV.History(context.Background(), "topic")
	if err != nil {
		t.Fatalf("KV.History: %v", err)
	}
	if len(versions) != 2 {
		t.Fatalf("len(versions) = %d, want 2", len(versions))
	}
	if versions[1].Version != 2 {
		t.Errorf("versions[1].Version = %d, want 2", versions[1].Version)
	}
}

// TestKVGetNotFound exercises the 404 → typed-error path.
func TestKVGetNotFound(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	ms.Error("GET", "/v1/workspaces/ws_test/kv/missing", 404, "KEY_NOT_FOUND", "key missing not found")

	_, err := ws.KV.Get(context.Background(), "missing")
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, plinth.ErrKeyNotFound) {
		t.Errorf("err = %v, want ErrKeyNotFound", err)
	}
}

// TestKVDelete covers the tombstone path.
func TestKVDelete(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	ms.On("DELETE", "/v1/workspaces/ws_test/kv/foo", func(_ recordedRequest) mockResponse {
		return mockResponse{Status: 204}
	})
	if err := ws.KV.Delete(context.Background(), "foo"); err != nil {
		t.Fatalf("KV.Delete: %v", err)
	}
}

// TestFilesWriteAndRead covers binary upload + download round-trip.
func TestFilesWriteAndRead(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)

	ms.JSON("PUT", "/v1/workspaces/ws_test/files/report.md", 200, map[string]any{
		"workspace_id": "ws_test",
		"path":         "report.md",
		"size":         12,
		"sha256":       "deadbeef",
		"content_type": "text/plain; charset=utf-8",
		"version":      1,
		"created_at":   now,
		"deleted":      false,
	})
	ms.Bytes("GET", "/v1/workspaces/ws_test/files/report.md", 200, []byte("# Report\n..."), "text/plain; charset=utf-8")

	entry, err := ws.Files.WriteText(context.Background(), "report.md", "# Report\n...", nil)
	if err != nil {
		t.Fatalf("Files.WriteText: %v", err)
	}
	if entry.Version != 1 {
		t.Errorf("entry.Version = %d, want 1", entry.Version)
	}

	body, err := ws.Files.Read(context.Background(), "report.md")
	if err != nil {
		t.Fatalf("Files.Read: %v", err)
	}
	if string(body) != "# Report\n..." {
		t.Errorf("body = %q, want # Report\\n...", string(body))
	}

	// Verify Content-Type header on the upload.
	put := findRequest(ms.Requests(), "PUT", "/v1/workspaces/ws_test/files/report.md")
	if put == nil {
		t.Fatal("expected PUT request, got none")
	}
	if !strings.HasPrefix(put.Headers.Get("Content-Type"), "text/plain") {
		t.Errorf("Content-Type = %q, want text/plain prefix", put.Headers.Get("Content-Type"))
	}
}

// TestFilesReadText is the convenience wrapper for UTF-8 file reads.
func TestFilesReadText(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	ms.Bytes("GET", "/v1/workspaces/ws_test/files/notes.txt", 200, []byte("hello world"), "text/plain")

	got, err := ws.Files.ReadText(context.Background(), "notes.txt")
	if err != nil {
		t.Fatalf("Files.ReadText: %v", err)
	}
	if got != "hello world" {
		t.Errorf("ReadText = %q, want hello world", got)
	}
}

// TestFilesReadNotFound verifies the typed error when fetching a
// missing path.
func TestFilesReadNotFound(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	ms.Error("GET", "/v1/workspaces/ws_test/files/missing.txt", 404, "FILE_NOT_FOUND", "missing.txt")

	_, err := ws.Files.Read(context.Background(), "missing.txt")
	if !errors.Is(err, plinth.ErrFileNotFound) {
		t.Errorf("err = %v, want ErrFileNotFound", err)
	}
}

// TestSnapshotCreate covers the snapshot lifecycle.
func TestSnapshotCreate(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)

	ms.JSON("POST", "/v1/workspaces/ws_test/snapshots", 201, map[string]any{
		"id":            "snap_1",
		"workspace_id":  "ws_test",
		"name":          "baseline",
		"message":       "initial state",
		"created_at":    now,
		"kv_versions":   map[string]any{},
		"file_versions": map[string]any{},
	})

	snap, err := ws.Snapshot(context.Background(), "baseline", "initial state")
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if snap.ID != "snap_1" {
		t.Errorf("snap.ID = %q, want snap_1", snap.ID)
	}

	post := findRequest(ms.Requests(), "POST", "/v1/workspaces/ws_test/snapshots")
	if post == nil {
		t.Fatal("expected POST snapshot request, got none")
	}
	var body map[string]any
	post.jsonBody(t, &body)
	if body["name"] != "baseline" || body["message"] != "initial state" {
		t.Errorf("body = %v, want name=baseline message=\"initial state\"", body)
	}
}

// TestBranchCreateAndList covers the branch endpoint pair.
func TestBranchCreateAndList(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)

	ms.JSON("POST", "/v1/workspaces/ws_test/branches", 201, map[string]any{
		"id":               "br_1",
		"workspace_id":     "ws_test",
		"name":             "experiment",
		"from_snapshot_id": "snap_1",
		"created_at":       now,
		"merged":           false,
	})
	ms.JSON("GET", "/v1/workspaces/ws_test/branches", 200, map[string]any{
		"branches": []any{
			map[string]any{
				"id":               "br_1",
				"workspace_id":     "ws_test",
				"name":             "experiment",
				"from_snapshot_id": "snap_1",
				"created_at":       now,
				"merged":           false,
			},
		},
	})

	br, err := ws.Branch(context.Background(), "experiment", "snap_1")
	if err != nil {
		t.Fatalf("Branch: %v", err)
	}
	if br.ID != "br_1" {
		t.Errorf("br.ID = %q, want br_1", br.ID)
	}

	all, err := ws.Branches(context.Background())
	if err != nil {
		t.Fatalf("Branches: %v", err)
	}
	if len(all) != 1 || all[0].ID != "br_1" {
		t.Errorf("Branches = %v, want one branch br_1", all)
	}
}

// TestWithBranchScopesQuery verifies the WithBranch view appends
// ?branch=<id> to subsequent KV writes.
func TestWithBranchScopesQuery(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("PUT", "/v1/workspaces/ws_test/kv/foo", 200, map[string]any{
		"workspace_id": "ws_test", "key": "foo", "value": "v",
		"version": 1, "created_at": now, "deleted": false,
	})

	branched := ws.WithBranch("br_test")
	if branched.BranchID() != "br_test" {
		t.Errorf("BranchID = %q, want br_test", branched.BranchID())
	}
	if _, err := branched.KV.Set(context.Background(), "foo", "v"); err != nil {
		t.Fatalf("KV.Set on branch: %v", err)
	}
	last := ms.LastRequest(t)
	if !strings.Contains(last.Query, "branch=br_test") {
		t.Errorf("query = %q, want branch=br_test", last.Query)
	}
}

// TestChannelsSendAndReceive covers the v0.2 channel surface.
func TestChannelsSendAndReceive(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)

	ms.JSON("POST", "/v1/workspaces/ws_test/channels/results/send", 201, map[string]any{
		"id":           "msg_1",
		"channel":      "results",
		"workspace_id": "ws_test",
		"seq":          1,
		"payload":      map[string]any{"text": "done"},
		"sent_at":      now,
	})
	ms.JSON("GET", "/v1/workspaces/ws_test/channels/results/receive", 200, map[string]any{
		"messages": []any{
			map[string]any{
				"id":           "msg_1",
				"channel":      "results",
				"workspace_id": "ws_test",
				"seq":          1,
				"payload":      map[string]any{"text": "done"},
				"sent_at":      now,
			},
		},
	})

	msg, err := ws.Channels.Send(context.Background(), "results",
		map[string]any{"text": "done"},
		plinth.ChannelSendOpts{Sender: "agent-A", Type: "result"},
	)
	if err != nil {
		t.Fatalf("Channels.Send: %v", err)
	}
	if msg.ID != "msg_1" {
		t.Errorf("msg.ID = %q, want msg_1", msg.ID)
	}

	got, err := ws.Channels.Receive(context.Background(), "results",
		plinth.ChannelReceiveOpts{Consumer: "writer", Limit: 10},
	)
	if err != nil {
		t.Fatalf("Channels.Receive: %v", err)
	}
	if len(got) != 1 || got[0].ID != "msg_1" {
		t.Errorf("got = %v, want one message msg_1", got)
	}

	// Verify ?consumer=writer&limit=10 was attached.
	rec := findRequest(ms.Requests(), "GET", "/v1/workspaces/ws_test/channels/results/receive")
	if rec == nil {
		t.Fatal("no receive request recorded")
	}
	if !strings.Contains(rec.Query, "consumer=writer") || !strings.Contains(rec.Query, "limit=10") {
		t.Errorf("query = %q, want consumer=writer&limit=10", rec.Query)
	}
}

// TestWorkflowsCreateAndStartStep covers a basic workflow round-trip.
func TestWorkflowsCreateAndStartStep(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)

	ms.JSON("POST", "/v1/workspaces/ws_test/workflows", 201, map[string]any{
		"id":             "wf_1",
		"workspace_id":   "ws_test",
		"name":           "pipeline",
		"steps_manifest": []any{"a", "b", "c"},
		"steps":          []any{},
		"status":         "pending",
		"created_at":     now,
	})
	ms.JSON("POST", "/v1/workspaces/ws_test/workflows/wf_1/steps", 201, map[string]any{
		"id":          "step_a",
		"workflow_id": "wf_1",
		"name":        "a",
		"status":      "running",
		"attempt":     1,
		"started_at":  now,
	})

	wf, err := ws.Workflows.Create(context.Background(), "pipeline", []string{"a", "b", "c"})
	if err != nil {
		t.Fatalf("Workflows.Create: %v", err)
	}
	if wf.ID() != "wf_1" {
		t.Errorf("wf.ID = %q, want wf_1", wf.ID())
	}
	step, err := wf.StartStep(context.Background(), "a", map[string]any{"x": 1})
	if err != nil {
		t.Fatalf("StartStep: %v", err)
	}
	if step.ID != "step_a" || step.Status != plinth.WorkflowStatusRunning {
		t.Errorf("step = %+v, want id=step_a status=running", step)
	}
}

// TestWorkflowStartStepRejectsOffManifest exercises the client-side
// validation: an unknown step name fails before any HTTP call.
func TestWorkflowStartStepRejectsOffManifest(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("POST", "/v1/workspaces/ws_test/workflows", 201, map[string]any{
		"id":             "wf_1",
		"workspace_id":   "ws_test",
		"name":           "pipeline",
		"steps_manifest": []any{"a", "b"},
		"steps":          []any{},
		"status":         "pending",
		"created_at":     now,
	})

	wf, err := ws.Workflows.Create(context.Background(), "pipeline", []string{"a", "b"})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	_, err = wf.StartStep(context.Background(), "z", nil)
	if err == nil {
		t.Fatal("expected error for off-manifest step name, got nil")
	}
	if !errors.Is(err, plinth.ErrInvalidWorkflowStep) {
		t.Errorf("err = %v, want ErrInvalidWorkflowStep", err)
	}
}

// TestWorkflowLeaseStepReturnsNilOnConflict checks that a 409
// LEASE_CONFLICT surfaces as (nil, nil) so worker code can branch
// cleanly without a typed-error import.
func TestWorkflowLeaseStepReturnsNilOnConflict(t *testing.T) {
	_, ws, ms := workspaceFixture(t)
	now := time.Now().Format(time.RFC3339Nano)
	ms.JSON("POST", "/v1/workspaces/ws_test/workflows", 201, map[string]any{
		"id":             "wf_1",
		"workspace_id":   "ws_test",
		"name":           "p",
		"steps_manifest": []any{"a"},
		"steps":          []any{},
		"status":         "pending",
		"created_at":     now,
	})
	ms.Error("POST", "/v1/workspaces/ws_test/workflows/wf_1/steps/step_a/lease",
		409, "LEASE_CONFLICT", "step already leased by another worker")

	wf, _ := ws.Workflows.Create(context.Background(), "p", []string{"a"})
	lease, err := wf.LeaseStep(context.Background(), "step_a", "worker-1", 60)
	if err != nil {
		t.Fatalf("LeaseStep returned err = %v on 409, want nil", err)
	}
	if lease != nil {
		t.Errorf("lease = %+v, want nil on 409", lease)
	}
}
