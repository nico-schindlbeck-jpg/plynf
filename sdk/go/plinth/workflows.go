// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"errors"
)

// WorkflowsProxy is the v0.2 durable workflow surface for a workspace.
//
// Returns *WorkflowHandle from every non-list method so callers can
// chain step transitions in one expression.
type WorkflowsProxy struct {
	ws *WorkspaceClient
}

func newWorkflowsProxy(ws *WorkspaceClient) *WorkflowsProxy { return &WorkflowsProxy{ws: ws} }

// Create starts a new workflow with the given step manifest. metadata
// may be nil.
func (w *WorkflowsProxy) Create(ctx context.Context, name string, steps []string) (*WorkflowHandle, error) {
	return w.CreateWithMetadata(ctx, name, steps, nil)
}

// CreateWithMetadata is the variant of Create that accepts an
// arbitrary metadata bag persisted alongside the workflow.
func (w *WorkflowsProxy) CreateWithMetadata(ctx context.Context, name string, steps []string, metadata map[string]any) (*WorkflowHandle, error) {
	body := map[string]any{
		"name":  name,
		"steps": steps,
	}
	if metadata != nil {
		body["metadata"] = metadata
	}
	var wf Workflow
	err := w.ws.http.PostJSON(
		ctx,
		w.basePath(),
		&wf,
		WithJSON(body),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return newWorkflowHandle(w.ws, wf), nil
}

// Get fetches a workflow by ID.
func (w *WorkflowsProxy) Get(ctx context.Context, workflowID string) (*WorkflowHandle, error) {
	var wf Workflow
	err := w.ws.http.GetJSON(
		ctx,
		w.basePath()+"/"+EncodePathSegment(workflowID),
		&wf,
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return newWorkflowHandle(w.ws, wf), nil
}

// List returns every workflow on the workspace as bare models. Use
// Get to act on a workflow (it returns a *WorkflowHandle).
func (w *WorkflowsProxy) List(ctx context.Context) ([]Workflow, error) {
	var resp struct {
		Workflows []Workflow `json:"workflows"`
	}
	err := w.ws.http.GetJSON(
		ctx,
		w.basePath(),
		&resp,
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Workflows, nil
}

// GetOrCreate is an idempotent create-by-name. If a workflow with
// `name` already exists, returns it; otherwise creates one with the
// supplied manifest.
func (w *WorkflowsProxy) GetOrCreate(ctx context.Context, name string, steps []string) (*WorkflowHandle, error) {
	all, err := w.List(ctx)
	if err != nil {
		return nil, err
	}
	for _, wf := range all {
		if wf.Name == name {
			return w.Get(ctx, wf.ID)
		}
	}
	return w.Create(ctx, name, steps)
}

func (w *WorkflowsProxy) basePath() string {
	return "/v1/workspaces/" + EncodePathSegment(w.ws.ID()) + "/workflows"
}

// ---------------------------------------------------------------------------
// WorkflowHandle — methods on a single workflow
// ---------------------------------------------------------------------------

// WorkflowHandle wraps a *Workflow with method-style step transitions
// plus refresh/resume/lease helpers. Returned by every WorkflowsProxy
// method except List.
//
// Holds the parent workspace's HTTPClient by reference so callers
// don't have to thread anything through. Mutations
// (StartStep / CompleteStep / ...) refresh the cached model so
// subsequent reads against the handle are consistent.
type WorkflowHandle struct {
	ws *WorkspaceClient
	wf Workflow
}

func newWorkflowHandle(ws *WorkspaceClient, wf Workflow) *WorkflowHandle {
	return &WorkflowHandle{ws: ws, wf: wf}
}

// ID returns the workflow ID (e.g. "wf_01H…").
func (h *WorkflowHandle) ID() string { return h.wf.ID }

// Name returns the human-readable workflow name.
func (h *WorkflowHandle) Name() string { return h.wf.Name }

// Status returns the cached workflow status.
func (h *WorkflowHandle) Status() WorkflowStatus { return h.wf.Status }

// StepsManifest returns the expected step names in declaration order.
func (h *WorkflowHandle) StepsManifest() []string { return h.wf.StepsManifest }

// Steps returns the cached step log.
func (h *WorkflowHandle) Steps() []WorkflowStep { return h.wf.Steps }

// Model returns the cached *Workflow model. Use Refresh to re-fetch.
func (h *WorkflowHandle) Model() Workflow { return h.wf }

// StartStepOpts customises a single StartStep call.
type StartStepOpts struct {
	// Input is an optional payload recorded on the step.
	Input any
	// SnapshotID is an optional snapshot taken before the step ran.
	SnapshotID string
	// InitialStatus, when "pending", stages the step for a v0.5
	// durable worker to lease and run. Defaults to "running" for the
	// in-process flow.
	InitialStatus string
}

// StartStep records a new step on the workflow and returns it. Validates
// name against the manifest client-side so callers get a synchronous
// error before paying for the HTTP roundtrip.
func (h *WorkflowHandle) StartStep(ctx context.Context, name string, input any) (*WorkflowStep, error) {
	return h.StartStepWithOpts(ctx, name, StartStepOpts{Input: input})
}

// StartStepWithOpts is the variant of StartStep that exposes the full
// option struct (snapshot binding, initial status).
func (h *WorkflowHandle) StartStepWithOpts(ctx context.Context, name string, opts StartStepOpts) (*WorkflowStep, error) {
	if len(h.wf.StepsManifest) > 0 && !contains(h.wf.StepsManifest, name) {
		return nil, &PlinthError{
			Code:    ErrInvalidWorkflowStep.Code,
			Message: "step " + name + " is not declared in the workflow manifest",
		}
	}
	body := map[string]any{"name": name}
	if opts.Input != nil {
		body["input"] = opts.Input
	}
	if opts.SnapshotID != "" {
		body["snapshot_id"] = opts.SnapshotID
	}
	if opts.InitialStatus != "" {
		body["initial_status"] = opts.InitialStatus
	}
	var step WorkflowStep
	err := h.ws.http.PostJSON(
		ctx,
		h.stepsPath(),
		&step,
		WithJSON(body),
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	h.recordStep(step)
	return &step, nil
}

// CompleteStep marks stepID completed. snapshotID is the canonical
// resume point — pass "" to skip.
func (h *WorkflowHandle) CompleteStep(ctx context.Context, stepID string, output any, snapshotID string) (*WorkflowStep, error) {
	body := map[string]any{"status": "completed"}
	if output != nil {
		body["output"] = output
	}
	if snapshotID != "" {
		body["snapshot_id"] = snapshotID
	}
	return h.patchStep(ctx, stepID, body)
}

// FailStep marks stepID failed with a free-text error string.
func (h *WorkflowHandle) FailStep(ctx context.Context, stepID, errorMsg string) (*WorkflowStep, error) {
	return h.patchStep(ctx, stepID, map[string]any{
		"status": "failed",
		"error":  errorMsg,
	})
}

// CancelStep marks stepID cancelled.
func (h *WorkflowHandle) CancelStep(ctx context.Context, stepID string) (*WorkflowStep, error) {
	return h.patchStep(ctx, stepID, map[string]any{"status": "cancelled"})
}

// Cancel cancels the entire workflow on the server and refreshes the
// cached model.
func (h *WorkflowHandle) Cancel(ctx context.Context) error {
	var updated Workflow
	err := h.ws.http.PostJSON(
		ctx,
		h.workflowPath()+"/cancel",
		&updated,
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return err
	}
	h.wf = updated
	return nil
}

// ResumeInfo returns the next pending step plus the snapshot to
// restore from. Crash → restart → call this → restore from
// SnapshotID → continue at NextStep.
func (h *WorkflowHandle) ResumeInfo(ctx context.Context) (*ResumeInfo, error) {
	var info ResumeInfo
	err := h.ws.http.GetJSON(
		ctx,
		h.workflowPath()+"/resume",
		&info,
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &info, nil
}

// Refresh re-fetches the workflow (with its full step log) from the
// server, replacing the cached model.
func (h *WorkflowHandle) Refresh(ctx context.Context) error {
	var wf Workflow
	err := h.ws.http.GetJSON(
		ctx,
		h.workflowPath(),
		&wf,
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return err
	}
	h.wf = wf
	return nil
}

// PendingSteps returns steps in `pending` status — ready for a worker
// to lease.
func (h *WorkflowHandle) PendingSteps(ctx context.Context) ([]WorkflowStep, error) {
	var resp struct {
		Steps []WorkflowStep `json:"steps"`
	}
	err := h.ws.http.GetJSON(
		ctx,
		h.workflowPath()+"/pending",
		&resp,
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Steps, nil
}

// ExpiredLeases returns leases past their expiry that haven't been
// reaped yet.
func (h *WorkflowHandle) ExpiredLeases(ctx context.Context) ([]Lease, error) {
	var resp struct {
		Leases []Lease `json:"leases"`
	}
	err := h.ws.http.GetJSON(
		ctx,
		h.workflowPath()+"/expired",
		&resp,
		WithNotFoundCode(ErrWorkflowNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Leases, nil
}

// LeaseStep tries to lease stepID for workerID. Returns (nil, nil) on
// 409 LEASE_CONFLICT (someone else got it). Other errors surface
// normally.
func (h *WorkflowHandle) LeaseStep(ctx context.Context, stepID, workerID string, ttlSeconds int) (*Lease, error) {
	if ttlSeconds <= 0 {
		ttlSeconds = 60
	}
	var lease Lease
	err := h.ws.http.PostJSON(
		ctx,
		h.stepsPath()+"/"+EncodePathSegment(stepID)+"/lease",
		&lease,
		WithJSON(map[string]any{
			"worker_id":   workerID,
			"ttl_seconds": ttlSeconds,
		}),
		WithNotFoundCode(ErrWorkflowStepNotFound.Code),
	)
	if err != nil {
		if errors.Is(err, ErrLeaseConflict) {
			return nil, nil
		}
		return nil, err
	}
	return &lease, nil
}

// HeartbeatStep extends the lease on stepID. Must be called by the
// holding worker.
func (h *WorkflowHandle) HeartbeatStep(ctx context.Context, stepID, workerID string, ttlSeconds int) (*Lease, error) {
	body := map[string]any{"worker_id": workerID}
	if ttlSeconds > 0 {
		body["ttl_seconds"] = ttlSeconds
	}
	var lease Lease
	err := h.ws.http.PostJSON(
		ctx,
		h.stepsPath()+"/"+EncodePathSegment(stepID)+"/heartbeat",
		&lease,
		WithJSON(body),
		WithNotFoundCode(ErrWorkflowStepNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &lease, nil
}

// ReleaseStep releases the lease on stepID, marking the step `status`.
// status is typically "completed" or "failed".
func (h *WorkflowHandle) ReleaseStep(ctx context.Context, stepID, workerID string, status string, output any, errorMsg, snapshotID string) (*Lease, error) {
	body := map[string]any{
		"worker_id": workerID,
		"status":    status,
	}
	if output != nil {
		body["output"] = output
	}
	if errorMsg != "" {
		body["error"] = errorMsg
	}
	if snapshotID != "" {
		body["snapshot_id"] = snapshotID
	}
	var lease Lease
	err := h.ws.http.PostJSON(
		ctx,
		h.stepsPath()+"/"+EncodePathSegment(stepID)+"/release",
		&lease,
		WithJSON(body),
		WithNotFoundCode(ErrWorkflowStepNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	// Best-effort refresh so callers reading h.Steps after a release
	// see the new lifecycle. Failure is silent — a refresh hiccup
	// shouldn't mask a successful release.
	_ = h.Refresh(ctx)
	return &lease, nil
}

func (h *WorkflowHandle) patchStep(ctx context.Context, stepID string, body map[string]any) (*WorkflowStep, error) {
	var step WorkflowStep
	err := h.ws.http.PatchJSON(
		ctx,
		h.stepsPath()+"/"+EncodePathSegment(stepID),
		&step,
		WithJSON(body),
		WithNotFoundCode(ErrWorkflowStepNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	h.recordStep(step)
	return &step, nil
}

func (h *WorkflowHandle) recordStep(step WorkflowStep) {
	for i := range h.wf.Steps {
		if h.wf.Steps[i].ID == step.ID {
			h.wf.Steps[i] = step
			return
		}
	}
	h.wf.Steps = append(h.wf.Steps, step)
}

func (h *WorkflowHandle) workflowPath() string {
	return "/v1/workspaces/" + EncodePathSegment(h.ws.ID()) + "/workflows/" + EncodePathSegment(h.wf.ID)
}

func (h *WorkflowHandle) stepsPath() string {
	return h.workflowPath() + "/steps"
}

// contains reports whether haystack contains needle. Tiny helper to
// avoid pulling in golang.org/x/exp/slices for a single check.
func contains(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}
