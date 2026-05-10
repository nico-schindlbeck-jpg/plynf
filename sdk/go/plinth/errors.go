// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

// Package plinth — typed error hierarchy mirroring CONTRACTS.md.
//
// Every HTTP error returned by a Plinth service lands as a *PlinthError
// whose Code matches the service envelope's "error.code" field. A small
// set of pre-declared sentinel errors lets callers route on the code via
// errors.Is — for example:
//
//	if errors.Is(err, plinth.ErrToolNotFound) {
//	    // tool was not registered
//	}
//
// All sentinels share a single underlying type so the SDK can return
// rich error values (with Details, StatusCode, Body, retry hints) while
// still satisfying the standard "errors.Is" contract.
package plinth

import (
	"fmt"
)

// PlinthError is the single error type returned by every SDK call.
//
// Sentinel values declared at package scope (ErrWorkspaceNotFound,
// ErrToolNotFound, …) carry only a Code and act as targets for
// errors.Is. Errors raised from real HTTP responses additionally carry
// Message, Details, StatusCode, and Body so callers can inspect the
// failure without re-reading the response.
type PlinthError struct {
	// Code is the stable string identifier from the service error
	// envelope (e.g. "WORKSPACE_NOT_FOUND"). Drives errors.Is dispatch.
	Code string

	// Message is the human-readable description from the response.
	Message string

	// Details is the structured payload (rate-limit metadata, schema
	// validator output, etc.). May be nil.
	Details map[string]any

	// StatusCode is the HTTP status of the underlying response, or 0
	// when the error never made it onto the wire (config validation,
	// network error before status is known).
	StatusCode int

	// Body is the raw response body, useful for debugging when the
	// envelope is malformed. May be nil.
	Body []byte

	// RetryAfter is populated for rate-limit errors (RATE_LIMITED /
	// COST_CAP_EXCEEDED). 0 when no hint was present.
	RetryAfter float64

	// LimitType (rate-limit only) is "rpm", "cost_hour", or "cost_day"
	// when the server reported it.
	LimitType string

	// Cause is the wrapped underlying error (e.g. a *url.Error from
	// the Go net stack). May be nil.
	Cause error
}

// Error implements the error interface.
func (e *PlinthError) Error() string {
	if e == nil {
		return "<nil>"
	}
	if e.Code != "" && e.Message != "" {
		return fmt.Sprintf("[%s] %s", e.Code, e.Message)
	}
	if e.Code != "" {
		return e.Code
	}
	return e.Message
}

// Unwrap supports errors.Unwrap so callers can reach the underlying
// network error (or any other wrapped cause).
func (e *PlinthError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Cause
}

// Is reports whether the receiver matches target. Two PlinthErrors match
// iff their Code is non-empty and equal — this lets the package-level
// sentinels (ErrToolNotFound etc.) act as standard errors.Is targets.
func (e *PlinthError) Is(target error) bool {
	if e == nil || target == nil {
		return false
	}
	t, ok := target.(*PlinthError)
	if !ok {
		return false
	}
	if t.Code == "" || e.Code == "" {
		return false
	}
	return e.Code == t.Code
}

// newError constructs a *PlinthError carrying a code and message.
// Used by the http layer when wiring a typed error onto a response.
func newError(code, message string) *PlinthError {
	return &PlinthError{Code: code, Message: message}
}

// Sentinel errors. Every code emitted by a Plinth service has a
// matching sentinel here so callers can use errors.Is to dispatch.
//
// Mirrors the maps in sdk/python/src/plinth/_http.py and
// sdk/typescript/src/errors.ts.
var (
	// 400 — validation
	ErrInvalidArguments = newError("INVALID_ARGUMENTS", "invalid arguments")
	ErrSchemaViolation  = newError("SCHEMA_VIOLATION", "schema violation")

	// 401 — auth
	ErrUnauthorized = newError("UNAUTHORIZED", "unauthorized")
	ErrInvalidToken = newError("INVALID_TOKEN", "invalid token")
	ErrTokenExpired = newError("TOKEN_EXPIRED", "token expired")
	ErrTokenRevoked = newError("TOKEN_REVOKED", "token revoked")

	// 404 — not found
	ErrWorkspaceNotFound    = newError("WORKSPACE_NOT_FOUND", "workspace not found")
	ErrKeyNotFound          = newError("KEY_NOT_FOUND", "key not found")
	ErrFileNotFound         = newError("FILE_NOT_FOUND", "file not found")
	ErrSnapshotNotFound     = newError("SNAPSHOT_NOT_FOUND", "snapshot not found")
	ErrBranchNotFound       = newError("BRANCH_NOT_FOUND", "branch not found")
	ErrToolNotFound         = newError("TOOL_NOT_FOUND", "tool not found")
	ErrChannelNotFound      = newError("CHANNEL_NOT_FOUND", "channel not found")
	ErrMessageNotFound      = newError("MESSAGE_NOT_FOUND", "message not found")
	ErrWorkflowNotFound     = newError("WORKFLOW_NOT_FOUND", "workflow not found")
	ErrWorkflowStepNotFound = newError("WORKFLOW_STEP_NOT_FOUND", "workflow step not found")
	ErrInvalidWorkflowStep  = newError("INVALID_WORKFLOW_STEP", "invalid workflow step")
	ErrSigningKeyNotFound   = newError("SIGNING_KEY_NOT_FOUND", "signing key not found")
	ErrLockNotFound         = newError("LOCK_NOT_FOUND", "lock not found")

	// 409 — conflicts
	ErrLeaseConflict = newError("LEASE_CONFLICT", "lease conflict")
	ErrLeaseNotHeld  = newError("LEASE_NOT_HELD", "lease not held")
	ErrLockConflict  = newError("LOCK_CONFLICT", "lock conflict")
	ErrLockNotHeld   = newError("LOCK_NOT_HELD", "lock not held")
	ErrWorkerNotFound = newError("WORKER_NOT_FOUND", "worker not found")

	// 429 — rate limits / cost caps
	ErrRateLimited     = newError("RATE_LIMITED", "rate limited")
	ErrCostCapExceeded = newError("COST_CAP_EXCEEDED", "cost cap exceeded")

	// Tool failures
	ErrToolInvocationFailed = newError("TOOL_INVOCATION_FAILED", "tool invocation failed")

	// Client-side / transport
	ErrInvalidConfig    = newError("INVALID_CONFIG", "invalid Plinth config")
	ErrConnectionError  = newError("CONNECTION_ERROR", "connection error")
	ErrInternal         = newError("INTERNAL_ERROR", "internal error")
	ErrIdentityNotConfigured = newError("IDENTITY_NOT_CONFIGURED", "identity service not configured")
)

// codeToSentinel maps every server-emitted code to the sentinel that
// should be set as Is-target on the returned error. The HTTP layer uses
// this for the typed-Is dispatch — an error returned for code
// "TOOL_NOT_FOUND" satisfies errors.Is(err, ErrToolNotFound).
//
// "LOCK_HELD" is the workspace service's wire code; we normalise it to
// "LOCK_CONFLICT" client-side.
var codeToSentinel = map[string]*PlinthError{
	"WORKSPACE_NOT_FOUND":     ErrWorkspaceNotFound,
	"KEY_NOT_FOUND":           ErrKeyNotFound,
	"FILE_NOT_FOUND":          ErrFileNotFound,
	"SNAPSHOT_NOT_FOUND":      ErrSnapshotNotFound,
	"BRANCH_NOT_FOUND":        ErrBranchNotFound,
	"TOOL_NOT_FOUND":          ErrToolNotFound,
	"CHANNEL_NOT_FOUND":       ErrChannelNotFound,
	"MESSAGE_NOT_FOUND":       ErrMessageNotFound,
	"WORKFLOW_NOT_FOUND":      ErrWorkflowNotFound,
	"WORKFLOW_STEP_NOT_FOUND": ErrWorkflowStepNotFound,
	"INVALID_WORKFLOW_STEP":   ErrInvalidWorkflowStep,
	"SIGNING_KEY_NOT_FOUND":   ErrSigningKeyNotFound,
	"LOCK_NOT_FOUND":          ErrLockNotFound,
	"LOCK_NOT_HELD":           ErrLockNotHeld,
	"LOCK_HELD":               ErrLockConflict,
	"LOCK_CONFLICT":           ErrLockConflict,
	"LEASE_CONFLICT":          ErrLeaseConflict,
	"LEASE_NOT_HELD":          ErrLeaseNotHeld,
	"WORKER_NOT_FOUND":        ErrWorkerNotFound,
	"INVALID_ARGUMENTS":       ErrInvalidArguments,
	"SCHEMA_VIOLATION":        ErrSchemaViolation,
	"UNAUTHORIZED":            ErrUnauthorized,
	"INVALID_TOKEN":           ErrInvalidToken,
	"TOKEN_EXPIRED":           ErrTokenExpired,
	"TOKEN_REVOKED":           ErrTokenRevoked,
	"RATE_LIMITED":            ErrRateLimited,
	"COST_CAP_EXCEEDED":       ErrCostCapExceeded,
	"TOOL_INVOCATION_FAILED":  ErrToolInvocationFailed,
}

// statusToCode is the fallback used when the server didn't include a
// machine-readable code in the envelope. Mirrors the Python SDK's
// _STATUS_TO_EXCEPTION map.
var statusToCode = map[int]string{
	400: "INVALID_ARGUMENTS",
	401: "UNAUTHORIZED",
	429: "RATE_LIMITED",
}
